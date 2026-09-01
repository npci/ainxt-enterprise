// SPDX-License-Identifier: Apache-2.0
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    ConnectionLineType,
    ReactFlow,
    Background,
    Controls,
    MiniMap,
    Panel,
    useReactFlow,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import Dagre from '@dagrejs/dagre';

import useWorkflowStore from '../../../store/workflowStore';
import StartNode from './nodes/StartNode';
import AgentNode from './nodes/AgentNode';
import EndNode from './nodes/EndNode';
import ConditionNode from './nodes/ConditionNode';
import SubflowNode from './nodes/SubflowNode';
import LoopNode from './nodes/LoopNode';
import EvaluationGateNode from './nodes/EvaluationGateNode';
import AiEdge from './edges/AiEdge';

const nodeTypes = {
    start: StartNode,
    agent: AgentNode,
    end: EndNode,
    condition: ConditionNode,
    subflow: SubflowNode,
    loop: LoopNode,
    // Loop Engineering P2: in-graph judge gate. Backend dispatcher lives
    // in NativeEngine._traverse → _route_evaluation_gate.
    evaluation_gate: EvaluationGateNode,
};

const edgeTypes = {
    'ai-edge': AiEdge,
};

/* ── Auto-layout via Dagre ────────────────────────────────── */

// Default fallback width used for edge-proximity hit-testing when a node
// hasn't been measured by React Flow yet.
const NODE_W = 200;
const NODE_H = 64;

// Per-type dimension fallbacks. The condition node is significantly
// taller because it renders the expression + ELSE rows inline.
const NODE_DIMENSIONS = {
    start:           { width: 180, height: 64 },
    end:             { width: 180, height: 64 },
    agent:           { width: 220, height: 80 },
    condition:       { width: 240, height: 160 },
    evaluation_gate: { width: 240, height: 140 },
};

function getNodeDimensions(node) {
    // Prefer the live measurement React Flow reports. `measured` is the
    // newer (v12+) API; `width`/`height` covers older versions. A small
    // safety margin (8px) is added so Dagre leaves breathing room between
    // ranks even when measurements are tight.
    const measuredW = node.measured?.width ?? node.width;
    const measuredH = node.measured?.height ?? node.height;
    const fallback = NODE_DIMENSIONS[node.type] || { width: NODE_W, height: NODE_H };
    return {
        width:  Math.max(measuredW || 0, fallback.width),
        height: Math.max(measuredH || 0, fallback.height) + 8,
    };
}

// Edges that must NOT drive the Dagre ranking. A loop node closes its body
// subgraph with a back-edge (agent → loop), which is a legitimate cycle in
// the workflow but poison for a rank-based layout: Dagre has no topological
// order for a cycle, so it scatters nodes (End floats into the middle, the
// loop body lands beside the branches, etc.). We rank on the acyclic forward
// subgraph only — the excluded edges still render as curves, they just don't
// distort node placement.
function isLayoutBackEdge(edge, nodesById) {
    const targetType = nodesById.get(edge.target)?.type;
    // Any edge pointing INTO a loop node is a body return path.
    if (targetType === 'loop') return true;
    return false;
}

function getLayoutedNodes(nodes, edges) {
    const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
    // Generous spacing so neighbouring ranks (especially branches under a
    // condition node) never overlap, even with the tallest body.
    g.setGraph({ rankdir: 'TB', nodesep: 80, ranksep: 110, marginx: 24, marginy: 24 });

    const nodesById = new Map(nodes.map((n) => [n.id, n]));

    const sizes = new Map();
    nodes.forEach((node) => {
        const { width, height } = getNodeDimensions(node);
        sizes.set(node.id, { width, height });
        g.setNode(node.id, { width, height });
    });

    // Longest forward path (in edges) from each node, computed over the
    // acyclic subgraph. Used to push a loop's `exit` target (End) below the
    // whole loop body so it doesn't float up beside the loop.
    const forwardEdges = edges.filter((edge) => !isLayoutBackEdge(edge, nodesById));
    const adj = new Map();
    forwardEdges.forEach((e) => {
        if (!adj.has(e.source)) adj.set(e.source, []);
        adj.get(e.source).push(e.target);
    });
    const depthCache = new Map();
    const depthFrom = (id, seen = new Set()) => {
        if (depthCache.has(id)) return depthCache.get(id);
        if (seen.has(id)) return 0; // guard against any residual cycle
        seen.add(id);
        let best = 0;
        for (const nxt of adj.get(id) || []) {
            best = Math.max(best, 1 + depthFrom(nxt, seen));
        }
        seen.delete(id);
        depthCache.set(id, best);
        return best;
    };

    // Rank on the forward (acyclic) subgraph only. Feeding loop back-edges
    // to Dagre creates cycles that break the ranking and scatter the nodes.
    forwardEdges.forEach((edge) => {
        const isExit =
            nodesById.get(edge.source)?.type === 'loop' &&
            (edge.sourceHandle || '') === 'exit';
        if (isExit) {
            // Push End below the deepest point of the loop body so the exit
            // path lands at the bottom of the graph, not next to the loop.
            const bodyDepth = depthFrom(edge.source);
            g.setEdge(edge.source, edge.target, { minlen: Math.max(1, bodyDepth) });
        } else {
            g.setEdge(edge.source, edge.target);
        }
    });

    Dagre.layout(g);

    return nodes.map((node) => {
        const pos = g.node(node.id);
        const { width, height } = sizes.get(node.id);
        return {
            ...node,
            // Dagre reports the node's CENTER; React Flow wants the top-left.
            position: { x: pos.x - width / 2, y: pos.y - height / 2 },
        };
    });
}

/* ── Edge proximity detection for drop-on-edge ────────────── */

function findNearestEdge(edges, nodes, point, threshold) {
    const nodeMap = new Map(nodes.map((n) => [n.id, n]));
    let closest = null;
    let closestDist = threshold;

    for (const edge of edges) {
        const sourceNode = nodeMap.get(edge.source);
        const targetNode = nodeMap.get(edge.target);
        if (!sourceNode || !targetNode) continue;

        const sx = sourceNode.position.x + NODE_W / 2;
        const sy = sourceNode.position.y + NODE_H;
        const tx = targetNode.position.x + NODE_W / 2;
        const ty = targetNode.position.y;

        const mx = (sx + tx) / 2;
        const my = (sy + ty) / 2;

        const dx = point.x - mx;
        const dy = point.y - my;
        const dist = Math.sqrt(dx * dx + dy * dy);

        if (dist < closestDist) {
            closestDist = dist;
            closest = edge;
        }
    }

    return closest;
}

/* ── MiniMap node colour helper ───────────────────────────── */

function minimapNodeColor(node) {
    // Softer fills tuned to the light-theme canvas; the minimap reads as
    // a quiet overview rather than a chart of saturated bars.
    switch (node.type) {
        case 'start': return '#0d9488';
        case 'agent': return '#4f46e5';
        case 'end': return '#059669';
        case 'condition': return '#d97706';
        case 'subflow': return '#6366f1';
        case 'loop': return '#0ea5e9';
        case 'evaluation_gate': return '#f59e0b';
        default: return '#6b7280';
    }
}

/* ── Canvas Component ─────────────────────────────────────── */

function Canvas({ onRequestEditMode }) {
    const reactFlowWrapper = useRef(null);
    const { screenToFlowPosition, fitView, getNodes } = useReactFlow();

    const nodes = useWorkflowStore((s) => s.nodes);
    const edges = useWorkflowStore((s) => s.edges);
    const onNodesChange = useWorkflowStore((s) => s.onNodesChange);
    const onEdgesChange = useWorkflowStore((s) => s.onEdgesChange);
    const onConnect = useWorkflowStore((s) => s.onConnect);
    const addNode = useWorkflowStore((s) => s.addNode);
    const setNodes = useWorkflowStore((s) => s.setNodes);
    const insertNodeOnEdge = useWorkflowStore((s) => s.insertNodeOnEdge);
    const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
    const setHoveredEdgeId = useWorkflowStore((s) => s.setHoveredEdgeId);

    /* ── Undo / Redo availability (subscribe to zundo temporal store) ─ */

    const [canUndo, setCanUndo] = useState(false);
    const [canRedo, setCanRedo] = useState(false);

    useEffect(() => {
        const temporal = useWorkflowStore.temporal;
        const sync = () => {
            const state = temporal.getState();
            setCanUndo(state.pastStates.length > 0);
            setCanRedo(state.futureStates.length > 0);
        };
        sync();
        const unsub = temporal.subscribe(sync);
        return () => unsub();
    }, []);

    /* ── Connection validation ─────────────────────────────── */

    const isValidConnection = useCallback(
        (connection) => {
            // No self-loops
            if (connection.source === connection.target) return false;
            // No duplicate edges
            const isDuplicate = edges.some(
                (e) => e.source === connection.source
                    && e.target === connection.target
                    && (e.sourceHandle || null) === (connection.sourceHandle || null)
                    && (e.targetHandle || null) === (connection.targetHandle || null)
            );
            return !isDuplicate;
        },
        [edges]
    );

    /* ── Drag & drop ───────────────────────────────────────── */

    const onDragOver = useCallback((event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
    }, []);

    const onDrop = useCallback(
        (event) => {
            event.preventDefault();
            const type = event.dataTransfer.getData('application/reactflow');
            if (!type) return;

            const position = screenToFlowPosition({
                x: event.clientX,
                y: event.clientY,
            });

            const nearbyEdge = findNearestEdge(edges, nodes, position, 50);
            if (nearbyEdge) {
                insertNodeOnEdge(nearbyEdge.id, type, position);
            } else {
                addNode(type, position);
            }
        },
        [screenToFlowPosition, addNode, insertNodeOnEdge, edges, nodes]
    );

    /* ── Selection ─────────────────────────────────────────── */

    const onNodeClick = useCallback(
        (event, node) => {
            setSelectedNode(node.id);
            // If we're in preview/chat mode, switch back to edit mode so the
            // ConfigPanel for the clicked node opens. This lets users jump
            // directly into an agent's config from the chat preview without
            // hunting for a pen icon.
            if (onRequestEditMode) onRequestEditMode();
        },
        [setSelectedNode, onRequestEditMode]
    );

    const onPaneClick = useCallback(() => setSelectedNode(null), [setSelectedNode]);

    /* ── Edge hover tracking ───────────────────────────────── */

    const onEdgeMouseEnter = useCallback(
        (_, edge) => setHoveredEdgeId(edge.id),
        [setHoveredEdgeId]
    );
    const onEdgeMouseLeave = useCallback(
        () => setHoveredEdgeId(null),
        [setHoveredEdgeId]
    );

    /* ── Auto-layout ───────────────────────────────────────── */

    const onAutoLayout = useCallback(() => {
        // Pull nodes from React Flow's internal store so we get accurate
        // `measured.width` / `measured.height` for each node (the Zustand
        // copy may not carry those measurements). We then merge those
        // dimensions back onto our source-of-truth `nodes` before laying
        // them out, so Dagre knows how tall the condition node really is.
        const rfNodes = getNodes();
        const measuredById = new Map(rfNodes.map((n) => [n.id, n.measured]));
        const enrichedNodes = nodes.map((n) => ({
            ...n,
            measured: n.measured || measuredById.get(n.id),
        }));

        const layouted = getLayoutedNodes(enrichedNodes, edges);
        setNodes(layouted);
        // Give React Flow a tick to measure, then fit
        requestAnimationFrame(() => fitView({ padding: 0.2, duration: 300 }));
    }, [nodes, edges, setNodes, fitView, getNodes]);

    /* ── Undo / Redo (Ctrl+Z / Ctrl+Shift+Z or Ctrl+Y) ──── */

    useEffect(() => {
        const handler = (e) => {
            // Don't capture when user is typing in an input/textarea
            const tag = e.target.tagName;
            if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;

            const isMod = e.ctrlKey || e.metaKey;

            if (isMod && e.key === 'z' && !e.shiftKey) {
                e.preventDefault();
                useWorkflowStore.temporal.getState().undo();
            } else if (isMod && (e.key === 'y' || (e.key === 'z' && e.shiftKey))) {
                e.preventDefault();
                useWorkflowStore.temporal.getState().redo();
            }
        };
        window.addEventListener('keydown', handler);
        return () => window.removeEventListener('keydown', handler);
    }, []);

    /* ── Edge normalization ────────────────────────────────── */

    const renderedEdges = useMemo(
        () => edges.map((edge) => {
            // eslint-disable-next-line no-unused-vars
            const { style: _style, ...rest } = edge;
            return { ...rest, type: 'ai-edge' };
        }),
        [edges]
    );

    /* ── Render ────────────────────────────────────────────── */

    return (
        <div className="canvas-container" ref={reactFlowWrapper}>
            <ReactFlow
                nodes={nodes}
                edges={renderedEdges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                isValidConnection={isValidConnection}
                onDragOver={onDragOver}
                onDrop={onDrop}
                onNodeClick={onNodeClick}
                onPaneClick={onPaneClick}
                onEdgeMouseEnter={onEdgeMouseEnter}
                onEdgeMouseLeave={onEdgeMouseLeave}
                nodeTypes={nodeTypes}
                edgeTypes={edgeTypes}
                deleteKeyCode={null}
                snapToGrid
                snapGrid={[28, 28]}
                proOptions={{ hideAttribution: true }}
                defaultViewport={{ x: 0, y: 0, zoom: 1 }}
                defaultEdgeOptions={{ type: 'ai-edge' }}
                connectionLineType={ConnectionLineType.Bezier}
                connectionLineStyle={{ stroke: '#4f46e5', strokeWidth: 2.25 }}
            >
                <Background color="rgba(116, 139, 170, 0.42)" gap={28} size={1.35} />

                <Controls showFitView={false} showInteractive={false} />

                <MiniMap
                    nodeColor={minimapNodeColor}
                    nodeStrokeColor="rgba(255, 255, 255, 0.85)"
                    nodeStrokeWidth={2}
                    nodeBorderRadius={4}
                    maskColor="rgba(15, 23, 42, 0.04)"
                    maskStrokeColor="rgba(79, 70, 229, 0.55)"
                    maskStrokeWidth={1.4}
                    pannable
                    zoomable
                    ariaLabel="Workflow overview"
                />

                {/* Canvas toolbar — top-right (avoids collision with MiniMap) */}
                <Panel position="top-right" className="canvas-toolbar">
                    <button
                        onClick={onAutoLayout}
                        title="Auto layout"
                        aria-label="Auto layout"
                        className="canvas-toolbar-btn"
                    >
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <rect x="3" y="3" width="7" height="7" rx="1.5" />
                            <rect x="14" y="3" width="7" height="7" rx="1.5" />
                            <rect x="3" y="14" width="7" height="7" rx="1.5" />
                            <rect x="14" y="14" width="7" height="7" rx="1.5" />
                        </svg>
                    </button>
                    <div className="canvas-toolbar-divider" />
                    <button
                        onClick={() => useWorkflowStore.temporal.getState().undo()}
                        title="Undo (Ctrl+Z)"
                        aria-label="Undo"
                        disabled={!canUndo}
                        className="canvas-toolbar-btn canvas-toolbar-history"
                    >
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M9 14L4 9l5-5" />
                            <path d="M4 9h10a6 6 0 0 1 6 6v0a6 6 0 0 1-6 6h-3" />
                        </svg>
                    </button>
                    <button
                        onClick={() => useWorkflowStore.temporal.getState().redo()}
                        title="Redo (Ctrl+Y)"
                        aria-label="Redo"
                        disabled={!canRedo}
                        className="canvas-toolbar-btn canvas-toolbar-history"
                    >
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M15 14l5-5-5-5" />
                            <path d="M20 9H10a6 6 0 0 0-6 6v0a6 6 0 0 0 6 6h3" />
                        </svg>
                    </button>
                </Panel>
            </ReactFlow>
        </div>
    );
}

export default Canvas;
