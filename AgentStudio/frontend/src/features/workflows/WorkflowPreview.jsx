// SPDX-License-Identifier: MIT
import { useCallback, useEffect, useMemo, useRef, useState, Fragment } from 'react';
import {
    ReactFlow,
    ReactFlowProvider,
    Background,
    Controls,
    MiniMap,
    Panel,
    addEdge,
    useReactFlow,
    applyNodeChanges,
    applyEdgeChanges,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import Dagre from '@dagrejs/dagre';

import StartNode from './editor/nodes/StartNode';
import AgentNode from './editor/nodes/AgentNode';
import EndNode from './editor/nodes/EndNode';
import ConditionNode from './editor/nodes/ConditionNode';
import SubflowNode from './editor/nodes/SubflowNode';
import LoopNode from './editor/nodes/LoopNode';
import EvaluationGateNode from './editor/nodes/EvaluationGateNode';
import AiEdge from './editor/edges/AiEdge';
import ConditionBuilder from './editor/conditions/ConditionBuilder';
import LoopWhileEditor from './editor/conditions/LoopWhileEditor';
import SubflowPicker from './editor/SubflowPicker';
import CatalogPicker from '../../components/common/CatalogPicker';
import KnowledgeSection, { KB_MODE_NONE } from '../../components/common/KnowledgeSection';
import useAvailableModels, { MODEL_STATUS } from '../../hooks/useAvailableModels';
import { DEFAULT_JUDGE_MODEL } from '../../config/models';
import useCurrentUser from '../../hooks/useCurrentUser';
import { stripProviderPrefix } from '../../utils/modelLabel';
import { getMaxTokensForModel } from '../../utils/modelMaxTokens';
import { makeId } from '../../utils/makeId';

// Same nodeTypes / edgeTypes the manual WorkflowEditor uses. The nodes read
// ``activeNodeIds`` / ``loopProgress`` from the global workflowStore but both
// default to empty arrays, so mounting them here without the store touching
// anything is safe — they just render without a run-glow.
const NODE_TYPES = {
    start: StartNode,
    agent: AgentNode,
    end: EndNode,
    condition: ConditionNode,
    subflow: SubflowNode,
    loop: LoopNode,
    evaluation_gate: EvaluationGateNode,
};
const EDGE_TYPES = { 'ai-edge': AiEdge };

const NEW_NODE_TYPES = [
    { type: 'agent',           label: 'Agent' },
    { type: 'condition',       label: 'Condition' },
    { type: 'loop',            label: 'Loop' },
    { type: 'evaluation_gate', label: 'Evaluation gate' },
    { type: 'subflow',         label: 'Subflow' },
    { type: 'end',             label: 'End' },
];

const NODE_DIMENSIONS = {
    start:           { width: 180, height: 64 },
    end:             { width: 180, height: 64 },
    agent:           { width: 220, height: 80 },
    condition:       { width: 240, height: 160 },
    evaluation_gate: { width: 240, height: 140 },
    loop:            { width: 220, height: 100 },
    subflow:         { width: 220, height: 90 },
};

// Auto-layout (same Dagre setup as Canvas.jsx, trimmed for preview use).
function layoutNodes(nodes, edges) {
    if (nodes.length === 0) return nodes;
    const g = new Dagre.graphlib.Graph({ compound: false });
    g.setDefaultEdgeLabel(() => ({}));
    g.setGraph({ rankdir: 'LR', nodesep: 60, ranksep: 90 });
    for (const n of nodes) {
        const dim = NODE_DIMENSIONS[n.type] || { width: 200, height: 64 };
        g.setNode(n.id, dim);
    }
    for (const e of edges) g.setEdge(e.source, e.target);
    Dagre.layout(g);
    return nodes.map((n) => {
        const p = g.node(n.id);
        return p
            ? { ...n, position: { x: p.x - (NODE_DIMENSIONS[n.type]?.width || 200) / 2,
                                  y: p.y - (NODE_DIMENSIONS[n.type]?.height || 64) / 2 } }
            : n;
    });
}

const HITL_MODES = [
    { value: 'off',             label: 'Off — no human review' },
    { value: 'after_response',  label: 'After response — review drafts before they go out' },
    { value: 'before_tool',     label: 'Before tool — approve every external action' },
    { value: 'both',            label: 'Both — approve tool calls and final response' },
];

const STOP_POLICIES = [
    { value: 'pass_or_max', label: 'Pass or max — stop when accepted or maxRetries hit' },
    { value: 'pass_only',   label: 'Pass only — retry until accepted' },
    { value: 'max_only',    label: 'Max only — always run maxRetries iterations' },
];

// ---------------------------------------------------------------------------
// WorkflowPreview — the right pane of the "Create workflow with AI" modal.
//
// Renders the assembled workflow as a live React Flow canvas + a per-node
// configuration form below it. The user can:
//   - Drag nodes to reposition (the modal keeps user-moved positions across
//     later chat updates).
//   - Click a node to open its config form (agent / condition / loop / gate
//     / subflow).
//   - Add nodes via the toolbar dropdown.
//   - Delete a node (Backspace when it's selected).
//   - Connect handles by dragging.
//   - Auto-layout via a button.
// Everything is controlled by ``nodes`` / ``edges`` / ``name`` props from the
// parent WorkflowFactoryChat — no global store is touched.
// ---------------------------------------------------------------------------
function WorkflowPreview({
    name,
    nodes,
    edges,
    onNameChange,
    onNodesChange,
    onEdgesChange,
    onDeploy,
    isDeploying,
    deployError,
}) {
    // Two-mode right pane. Default is ``'canvas'`` — the whole pane is the
    // React Flow graph. Clicking a node flips to ``'config'`` — the whole
    // pane becomes the selected node's configuration form. A Back button in
    // config mode returns to canvas mode. Only one mode is visible at a time
    // so the config form can be tall and comfortable to scroll.
    const [view, setView] = useState('canvas'); // 'canvas' | 'config'
    const [selectedNodeId, setSelectedNodeId] = useState(null);

    const selectedNode = useMemo(
        () => nodes.find((n) => n.id === selectedNodeId) || null,
        [nodes, selectedNodeId],
    );

    // If the currently-open config node disappears (deleted via chat / patch),
    // fall back to the canvas so we don't render an empty form.
    useEffect(() => {
        if (view === 'config' && !selectedNode) setView('canvas');
    }, [view, selectedNode]);

    const openNodeConfig = useCallback((nodeId) => {
        setSelectedNodeId(nodeId);
        setView('config');
    }, []);

    const backToCanvas = useCallback(() => {
        setView('canvas');
    }, []);

    const updateNodeData = useCallback((nodeId, dataPatch) => {
        onNodesChange(nodes.map((n) => (
            n.id === nodeId ? { ...n, data: { ...(n.data || {}), ...dataPatch } } : n
        )));
    }, [nodes, onNodesChange]);

    return (
        <div style={S.paneRoot}>
            <div style={S.paneHeader}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                    {view === 'config' && (
                        <button
                            type="button"
                            style={S.backBtn}
                            onClick={backToCanvas}
                            title="Back to workflow"
                        >
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M19 12H5M12 19l-7-7 7-7" />
                            </svg>
                            Back to workflow
                        </button>
                    )}
                    <div style={{ minWidth: 0 }}>
                        <div style={S.paneHeaderKicker}>
                            {view === 'config'
                                ? `Editing ${nodeLabel(selectedNode)}`
                                : 'Live preview'}
                        </div>
                        {view === 'canvas' ? (
                            <input
                                className="agent-field-input"
                                style={S.nameInput}
                                value={name}
                                onChange={(e) => onNameChange(e.target.value)}
                                placeholder="Workflow name"
                            />
                        ) : (
                            <div style={S.configHeaderSub}>
                                Change any field, then use <em style={{ color: '#4f46e5', fontStyle: 'normal', fontWeight: 600 }}>Back to workflow</em> to return to the graph.
                            </div>
                        )}
                    </div>
                </div>
                <button
                    type="button"
                    style={S.deployBtn(isDeploying)}
                    onClick={onDeploy}
                    disabled={isDeploying}
                    title="Save this workflow"
                >
                    {isDeploying ? (
                        <><span style={S.spinner} />Deploying…</>
                    ) : (
                        <>
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                <polyline points="20 6 9 17 4 12" />
                            </svg>
                            Deploy Workflow
                        </>
                    )}
                </button>
            </div>

            {deployError && <div style={S.deployError}>{deployError}</div>}

            {view === 'canvas' ? (
                <ReactFlowProvider>
                    <CanvasView
                        nodes={nodes}
                        edges={edges}
                        selectedNodeId={selectedNodeId}
                        onNodesChange={onNodesChange}
                        onEdgesChange={onEdgesChange}
                        onNodeClick={openNodeConfig}
                    />
                </ReactFlowProvider>
            ) : (
                <ConfigView
                    node={selectedNode}
                    onChange={(patch) => selectedNode && updateNodeData(selectedNode.id, patch)}
                />
            )}
        </div>
    );
}

function nodeLabel(node) {
    if (!node) return 'node';
    const d = node.data || {};
    const name = d.name || d.label || node.id;
    return `${name} (${node.type})`;
}

function CanvasView({ nodes, edges, selectedNodeId, onNodesChange, onEdgesChange, onNodeClick }) {
    const rf = useReactFlow();

    const handleNodesChange = useCallback((changes) => {
        onNodesChange(applyNodeChanges(changes, nodes));
    }, [nodes, onNodesChange]);
    const handleEdgesChange = useCallback((changes) => {
        onEdgesChange(applyEdgeChanges(changes, edges));
    }, [edges, onEdgesChange]);

    const handleConnect = useCallback((connection) => {
        const newEdge = {
            ...connection,
            id: `e-${connection.source}-${connection.sourceHandle || 'x'}-${connection.target}`,
            type: 'default',
            style: { stroke: '#6366f1', strokeWidth: 2 },
        };
        onEdgesChange(addEdge(newEdge, edges));
    }, [edges, onEdgesChange]);

    // Click a node → open its config form (via parent). Uses onNodeClick
    // rather than onSelectionChange so drags / marquee-selects don't kick us
    // out of canvas view.
    const handleNodeClick = useCallback((_ev, node) => {
        onNodeClick(node.id);
    }, [onNodeClick]);

    // Toolbar — add a new node of any type after the currently-selected node
    // (or after the start node when nothing is selected).
    const handleAddNode = useCallback((type) => {
        const existingIds = new Set(nodes.map((n) => n.id));
        const prefixMap = {
            agent: 'agent', condition: 'cond', loop: 'loop',
            evaluation_gate: 'gate', subflow: 'sub', end: 'end',
        };
        let idx = 1;
        while (existingIds.has(`${prefixMap[type] || 'node'}-${idx}`)) idx += 1;
        const newId = `${prefixMap[type] || 'node'}-${idx}`;

        const anchor = selectedNodeId
            ? nodes.find((n) => n.id === selectedNodeId)
            : nodes.find((n) => n.type === 'start') || nodes[0];
        const anchorPos = (anchor && anchor.position) || { x: 200, y: 200 };
        const newNode = {
            id: newId,
            type,
            position: { x: (anchorPos.x || 200) + 300, y: anchorPos.y || 200 },
            data: defaultNodeData(type, newId),
        };
        const nextNodes = [...nodes, newNode];

        // Splice edge: anchor → newNode → anchor's original successor.
        let nextEdges = edges;
        if (anchor) {
            const outgoing = edges.find((e) => e.source === anchor.id && !e.sourceHandle);
            if (outgoing) {
                nextEdges = edges.map((e) => (e === outgoing ? { ...e, target: newId } : e));
                nextEdges = [...nextEdges, {
                    id: `e-${newId}-${outgoing.target}`,
                    source: newId,
                    target: outgoing.target,
                    type: 'default',
                    style: { stroke: '#6366f1', strokeWidth: 2 },
                }];
            } else {
                nextEdges = [...edges, {
                    id: `e-${anchor.id}-${newId}`,
                    source: anchor.id, target: newId,
                    type: 'default',
                    style: { stroke: '#6366f1', strokeWidth: 2 },
                }];
            }
        }
        onNodesChange(nextNodes);
        onEdgesChange(nextEdges);
    }, [nodes, edges, selectedNodeId, onNodesChange, onEdgesChange]);

    const handleAutoLayout = useCallback(() => {
        onNodesChange(layoutNodes(nodes, edges));
        setTimeout(() => rf.fitView?.({ padding: 0.2 }), 60);
    }, [nodes, edges, onNodesChange, rf]);

    const handleFitView = useCallback(() => {
        rf.fitView?.({ padding: 0.2, duration: 300 });
    }, [rf]);

    return (
        <div style={S.canvasWrap}>
            <ReactFlow
                nodes={nodes}
                edges={edges}
                nodeTypes={NODE_TYPES}
                edgeTypes={EDGE_TYPES}
                onNodesChange={handleNodesChange}
                onEdgesChange={handleEdgesChange}
                onConnect={handleConnect}
                onNodeClick={handleNodeClick}
                fitView
                fitViewOptions={{ padding: 0.2 }}
                proOptions={{ hideAttribution: true }}
                minZoom={0.2}
                maxZoom={1.5}
                deleteKeyCode={['Delete', 'Backspace']}
            >
                <Background gap={16} size={1} />
                <Controls showInteractive={false} />
                <MiniMap
                    pannable
                    zoomable
                    nodeStrokeColor="#4f46e5"
                    nodeColor="#eef2ff"
                    maskColor="rgba(15,23,42,0.06)"
                />
                <Panel position="top-right" style={S.canvasToolbar}>
                    <AddNodeDropdown onSelect={handleAddNode} />
                    <button type="button" style={S.toolbarBtn} onClick={handleAutoLayout} title="Auto-arrange">
                        Auto-layout
                    </button>
                    <button type="button" style={S.toolbarBtn} onClick={handleFitView} title="Fit to view">
                        Fit view
                    </button>
                </Panel>
                <Panel position="bottom-left" style={S.canvasHintPanel}>
                    Click a node to edit its configuration.
                </Panel>
            </ReactFlow>
        </div>
    );
}

function ConfigView({ node, onChange }) {
    if (!node) {
        return (
            <div style={S.configFullScroll}>
                <div style={S.configEmpty}>
                    The node you were editing was removed. Use <strong>Back to workflow</strong> to pick another one.
                </div>
            </div>
        );
    }
    return (
        <div style={S.configFullScroll}>
            <NodeConfigForm key={node.id} node={node} onChange={onChange} />
        </div>
    );
}

function AddNodeDropdown({ onSelect }) {
    const [open, setOpen] = useState(false);
    const ref = useRef(null);
    useEffect(() => {
        if (!open) return undefined;
        const onDoc = (e) => {
            if (!ref.current?.contains(e.target)) setOpen(false);
        };
        document.addEventListener('mousedown', onDoc);
        return () => document.removeEventListener('mousedown', onDoc);
    }, [open]);
    return (
        <div ref={ref} style={{ position: 'relative' }}>
            <button type="button" style={S.toolbarBtn} onClick={() => setOpen((v) => !v)}>
                + Add node
            </button>
            {open && (
                <div style={S.addMenu}>
                    {NEW_NODE_TYPES.map((t) => (
                        <button
                            key={t.type}
                            type="button"
                            style={S.addMenuItem}
                            onClick={() => { setOpen(false); onSelect(t.type); }}
                        >
                            {t.label}
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}

function defaultNodeData(type, id) {
    switch (type) {
        case 'agent': return {
            name: 'New Agent',
            instructions: '',
            modelName: '',
            temperature: 0.3,
            maxTokens: 4096,
            topP: 0.9,
            tools: [],
            skills: [],
            hitlMode: 'off',
            knowledge: { mode: KB_MODE_NONE, namespaces: [] },
        };
        case 'condition': return { cases: [] };
        case 'loop': return { mode: 'for_each', itemsExpression: 'input.items', iteratorVar: 'item', maxIterations: 25 };
        case 'evaluation_gate': return {
            criteria: 'Response is factually accurate, complete, and cites sources',
            threshold: 0.85, stop_policy: 'pass_or_max',
            judgeModel: DEFAULT_JUDGE_MODEL, maxRetries: 3,
        };
        case 'subflow': return { kind: 'agent', refId: '', refName: '' };
        case 'end': return { label: 'End' };
        default: return {};
    }
}

// ---------------------------------------------------------------------------
// NodeConfigForm — switches by node.type. Each branch is a controlled form
// that mutates ``node.data`` via ``onChange``.
// ---------------------------------------------------------------------------
function NodeConfigForm({ node, onChange }) {
    const d = node.data || {};
    if (node.type === 'agent') return <AgentConfigForm data={d} onChange={onChange} />;
    if (node.type === 'condition') return <ConditionConfigForm data={d} onChange={onChange} />;
    if (node.type === 'loop') return <LoopConfigForm data={d} onChange={onChange} />;
    if (node.type === 'evaluation_gate') return <GateConfigForm data={d} onChange={onChange} />;
    if (node.type === 'subflow') return <SubflowConfigForm data={d} onChange={onChange} />;
    if (node.type === 'start' || node.type === 'end') {
        return (
            <div style={S.readOnly}>
                <div style={S.readOnlyTitle}>{node.type === 'start' ? 'Start' : 'End'} node</div>
                <div style={S.readOnlyBody}>Terminals have no editable configuration.</div>
            </div>
        );
    }
    return <div style={S.readOnly}>Unsupported node type.</div>;
}

function AgentConfigForm({ data, onChange }) {
    const {
        models: availableModels,
        providers: availableProviders,
        defaultModel,
        status: modelsStatus,
    } = useAvailableModels(data.modelName || '');
    const currentUser = useCurrentUser();

    return (
        <div style={S.formStack}>
            <div className="agent-config-section">
                <h2 className="agent-config-section-title">Agent</h2>

                <div className="agent-field">
                    <label className="agent-field-label">Name</label>
                    <input
                        type="text"
                        className="agent-field-input"
                        value={data.name || ''}
                        onChange={(e) => onChange({ name: e.target.value })}
                        placeholder="e.g. Reviewer"
                    />
                </div>

                <div className="agent-field">
                    <label className="agent-field-label">Instructions</label>
                    <textarea
                        className="agent-field-textarea"
                        value={data.instructions || ''}
                        onChange={(e) => onChange({ instructions: e.target.value })}
                        placeholder="Multi-section prompt: ## Role / ## Objective / ## Process / ## Do's / ## Don'ts / ## Output / ## Escalation"
                        rows={10}
                    />
                </div>
            </div>

            <div className="agent-config-section">
                <h2 className="agent-config-section-title">Model</h2>
                <div className="agent-field">
                    <label className="agent-field-label">Model</label>
                    <ModelPicker
                        value={data.modelName || defaultModel || ''}
                        models={availableModels}
                        providers={availableProviders}
                        defaultModel={defaultModel}
                        status={modelsStatus}
                        onChange={(m) => onChange({
                            modelName: m,
                            maxTokens: getMaxTokensForModel(m),
                        })}
                    />
                </div>
                <div className="agent-field">
                    <label className="agent-field-label">
                        Temperature
                        <span className="agent-field-hint">{Number(data.temperature ?? 0.3).toFixed(2)}</span>
                    </label>
                    <input
                        type="range"
                        className="agent-field-range"
                        min="0" max="1" step="0.01"
                        value={data.temperature ?? 0.3}
                        onChange={(e) => onChange({ temperature: parseFloat(e.target.value) })}
                    />
                </div>
                <div className="agent-field-row">
                    <div className="agent-field">
                        <label className="agent-field-label">Max tokens</label>
                        <input
                            type="number"
                            className="agent-field-input"
                            min="1"
                            max={getMaxTokensForModel(data.modelName || defaultModel)}
                            value={data.maxTokens ?? 4096}
                            onChange={(e) => onChange({ maxTokens: parseInt(e.target.value || '0', 10) })}
                        />
                    </div>
                    <div className="agent-field">
                        <label className="agent-field-label">Top P</label>
                        <input
                            type="number"
                            className="agent-field-input"
                            min="0" max="1" step="0.01"
                            value={data.topP ?? 0.9}
                            onChange={(e) => onChange({ topP: parseFloat(e.target.value) })}
                        />
                    </div>
                </div>
            </div>

            <div className="agent-config-section">
                <h2 className="agent-config-section-title">Tools &amp; Skills</h2>
                <CatalogPicker
                    kind="tools"
                    attached={(data.tools || []).map((t) => (typeof t === 'string' ? { name: t } : t))}
                    onChange={(next) => onChange({ tools: next })}
                />
                <CatalogPicker
                    kind="skills"
                    attached={(data.skills || []).map((s) => (typeof s === 'string' ? { name: s } : s))}
                    onChange={(next) => onChange({ skills: next })}
                />
            </div>

            <div className="agent-config-section">
                <h2 className="agent-config-section-title">Knowledge</h2>
                <KnowledgeSection
                    value={data.knowledge || { mode: KB_MODE_NONE }}
                    onChange={(kb) => onChange({ knowledge: kb })}
                    userDept={currentUser.department}
                    isApprover={currentUser.canApprove}
                    isAdmin={currentUser.role === 'admin'}
                />
            </div>

            <div className="agent-config-section">
                <h2 className="agent-config-section-title">Human-in-the-loop</h2>
                <div className="agent-field">
                    <label className="agent-field-label">
                        Approval mode
                        <span className="agent-field-hint">When should a human review the agent's actions?</span>
                    </label>
                    <select
                        className="agent-field-select"
                        value={data.hitlMode || 'off'}
                        onChange={(e) => onChange({ hitlMode: e.target.value })}
                    >
                        {HITL_MODES.map((m) => (
                            <option key={m.value} value={m.value}>{m.label}</option>
                        ))}
                    </select>
                </div>
                <div className="agent-field">
                    <label className="agent-field-label" style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                        <input
                            type="checkbox"
                            checked={!!data.enable_subagents}
                            onChange={(e) => onChange({ enable_subagents: e.target.checked })}
                        />
                        Allow subagent delegation (swarm) for this node
                    </label>
                </div>
            </div>
        </div>
    );
}

function ConditionConfigForm({ data, onChange }) {
    return (
        <div style={S.formStack}>
            <div className="agent-config-section">
                <h2 className="agent-config-section-title">Condition — routing rules</h2>
                <ConditionBuilder
                    cases={data.cases || []}
                    onChange={(cases) => onChange({ cases })}
                />
            </div>
        </div>
    );
}

function LoopConfigForm({ data, onChange }) {
    const mode = data.mode || 'for_each';
    return (
        <div style={S.formStack}>
            <div className="agent-config-section">
                <h2 className="agent-config-section-title">Loop</h2>

                <div className="agent-field">
                    <label className="agent-field-label">Mode</label>
                    <select
                        className="agent-field-select"
                        value={mode}
                        onChange={(e) => onChange({ mode: e.target.value })}
                    >
                        <option value="for_each">For each (iterate a list)</option>
                        <option value="count">Count (fixed number of times)</option>
                        <option value="while">While (until predicate is false)</option>
                    </select>
                </div>

                {mode === 'for_each' && (
                    <>
                        <div className="agent-field">
                            <label className="agent-field-label">Items expression</label>
                            <input
                                type="text"
                                className="agent-field-input"
                                value={data.itemsExpression || 'input.items'}
                                onChange={(e) => onChange({ itemsExpression: e.target.value })}
                                placeholder="input.items"
                            />
                        </div>
                        <div className="agent-field">
                            <label className="agent-field-label">Iterator variable</label>
                            <input
                                type="text"
                                className="agent-field-input"
                                value={data.iteratorVar || 'item'}
                                onChange={(e) => onChange({ iteratorVar: e.target.value })}
                                placeholder="item"
                            />
                        </div>
                    </>
                )}

                {mode === 'count' && (
                    <div className="agent-field">
                        <label className="agent-field-label">Count</label>
                        <input
                            type="number"
                            className="agent-field-input"
                            min="1" max="1000"
                            value={data.count ?? 3}
                            onChange={(e) => onChange({ count: parseInt(e.target.value || '1', 10) })}
                        />
                    </div>
                )}

                {mode === 'while' && (
                    <div className="agent-field">
                        <label className="agent-field-label">Continue while</label>
                        <LoopWhileEditor
                            cases={data.cases || []}
                            onChange={(cases) => onChange({ cases })}
                        />
                    </div>
                )}

                <div className="agent-field">
                    <label className="agent-field-label">Max iterations (safety cap)</label>
                    <input
                        type="number"
                        className="agent-field-input"
                        min="1" max="100"
                        value={data.maxIterations ?? 5}
                        onChange={(e) => onChange({ maxIterations: parseInt(e.target.value || '1', 10) })}
                    />
                </div>
            </div>
        </div>
    );
}

function GateConfigForm({ data, onChange }) {
    return (
        <div style={S.formStack}>
            <div className="agent-config-section">
                <h2 className="agent-config-section-title">Evaluation gate</h2>
                <div className="agent-field">
                    <label className="agent-field-label">Criteria</label>
                    <textarea
                        className="agent-field-textarea"
                        value={data.criteria || ''}
                        onChange={(e) => onChange({ criteria: e.target.value })}
                        placeholder="Plain-English rubric: what does 'good enough' look like?"
                        rows={4}
                    />
                </div>
                <div className="agent-field">
                    <label className="agent-field-label">
                        Threshold
                        <span className="agent-field-hint">{Number(data.threshold ?? 0.85).toFixed(2)}</span>
                    </label>
                    <input
                        type="range"
                        className="agent-field-range"
                        min="0" max="1" step="0.01"
                        value={data.threshold ?? 0.85}
                        onChange={(e) => onChange({ threshold: parseFloat(e.target.value) })}
                    />
                </div>
                <div className="agent-field">
                    <label className="agent-field-label">Stop policy</label>
                    <select
                        className="agent-field-select"
                        value={data.stop_policy || 'pass_or_max'}
                        onChange={(e) => onChange({ stop_policy: e.target.value })}
                    >
                        {STOP_POLICIES.map((p) => (
                            <option key={p.value} value={p.value}>{p.label}</option>
                        ))}
                    </select>
                </div>
                <div className="agent-field">
                    <label className="agent-field-label">Judge model</label>
                    <input
                        type="text"
                        className="agent-field-input"
                        value={data.judgeModel || DEFAULT_JUDGE_MODEL}
                        onChange={(e) => onChange({ judgeModel: e.target.value })}
                    />
                </div>
                <div className="agent-field">
                    <label className="agent-field-label">Max retries</label>
                    <input
                        type="number"
                        className="agent-field-input"
                        min="1" max="10"
                        value={data.maxRetries ?? 3}
                        onChange={(e) => onChange({ maxRetries: parseInt(e.target.value || '1', 10) })}
                    />
                </div>
            </div>
        </div>
    );
}

function SubflowConfigForm({ data, onChange }) {
    return (
        <div style={S.formStack}>
            <div className="agent-config-section">
                <h2 className="agent-config-section-title">Subflow — delegate to an existing asset</h2>
                <div className="agent-field">
                    <label className="agent-field-label">Kind</label>
                    <select
                        className="agent-field-select"
                        value={data.kind || 'agent'}
                        onChange={(e) => onChange({
                            kind: e.target.value,
                            refId: '',
                            refName: '',
                        })}
                    >
                        <option value="agent">Agent</option>
                        <option value="workflow">Workflow</option>
                    </select>
                </div>
                <div className="agent-field">
                    <label className="agent-field-label">Target</label>
                    <SubflowPicker
                        value={{ kind: data.kind || 'agent', refId: data.refId || '', refName: data.refName || '' }}
                        onChange={(next) => onChange({
                            kind: next.kind,
                            refId: next.refId,
                            refName: next.refName,
                        })}
                        mode="single"
                    />
                </div>
            </div>
        </div>
    );
}

// Inline model picker matching the one in AgentFactoryChat / AgentEditor so
// the workflow preview uses the same visual as everything else.
function ModelPicker({ value, models, providers, defaultModel, status, onChange }) {
    const [open, setOpen] = useState(false);
    const ref = useRef(null);

    const groups = useMemo(() => {
        if (Array.isArray(providers) && providers.length > 0) {
            return providers.map((g) => ({
                label: g.provider || '',
                options: (g.models || [])
                    .map((m) => (typeof m === 'string'
                        ? { id: m, label: stripProviderPrefix(m) }
                        : { id: m.id, label: m.label || stripProviderPrefix(m.id) }))
                    .filter((o) => o.id),
            })).filter((s) => s.options.length > 0);
        }
        const flat = (models && models.length ? models : [value || defaultModel])
            .filter(Boolean)
            .filter((m, i, arr) => arr.indexOf(m) === i);
        return flat.length > 0
            ? [{ label: '', options: flat.map((id) => ({ id, label: stripProviderPrefix(id) })) }]
            : [];
    }, [providers, models, value, defaultModel]);

    const flatIds = groups.flatMap((g) => g.options.map((o) => o.id));
    const selected = value || defaultModel || flatIds[0] || '';

    useEffect(() => {
        if (!open) return undefined;
        const onDoc = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
        document.addEventListener('mousedown', onDoc);
        return () => document.removeEventListener('mousedown', onDoc);
    }, [open]);

    return (
        <div className={`agent-model-inline${open ? ' open' : ''}`} ref={ref}>
            <button
                type="button"
                className="agent-model-inline-trigger"
                onClick={() => setOpen((v) => !v)}
                disabled={status === MODEL_STATUS.LOADING && flatIds.length === 0}
            >
                <span className="agent-model-inline-value">{stripProviderPrefix(selected)}</span>
                <svg className="agent-model-inline-chevron" width="16" height="16" viewBox="0 0 24 24"
                     fill="none" stroke="currentColor" strokeWidth="2"
                     strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="6 9 12 15 18 9" />
                </svg>
            </button>
            {open && (
                <div className="agent-model-inline-list" role="listbox">
                    {groups.map((group, i) => (
                        <Fragment key={group.label || `flat-${i}`}>
                            {group.label && <div className="agent-model-inline-group">{group.label}</div>}
                            {group.options.map((option) => (
                                <button
                                    key={option.id}
                                    type="button"
                                    className={`agent-model-inline-option${option.id === selected ? ' selected' : ''}`}
                                    onClick={() => { onChange(option.id); setOpen(false); }}
                                >
                                    {option.label}
                                </button>
                            ))}
                        </Fragment>
                    ))}
                </div>
            )}
        </div>
    );
}

const S = {
    paneRoot: {
        display: 'flex', flexDirection: 'column', flex: '1 1 auto', minHeight: 0, minWidth: 0,
        background: 'var(--color-surface, #ffffff)', overflow: 'hidden',
    },
    paneHeader: {
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px',
        padding: '14px 18px',
        borderBottom: '1px solid var(--color-border-subtle, #eef2f7)',
        background: '#fbfcfd', flexShrink: 0,
    },
    paneHeaderKicker: {
        fontSize: '11px', fontWeight: 600, color: '#64748b',
        textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '4px',
    },
    nameInput: { minWidth: '260px', fontWeight: 600 },
    deployBtn: (disabled) => ({
        display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '9px 16px',
        background: disabled ? '#c7d2fe' : '#4f46e5', border: 'none', borderRadius: '10px',
        color: '#fff', fontSize: '13px', fontWeight: 600,
        cursor: disabled ? 'not-allowed' : 'pointer', flexShrink: 0,
        boxShadow: disabled ? 'none' : '0 4px 12px rgba(99,102,241,0.35)',
    }),
    spinner: {
        width: '12px', height: '12px', borderRadius: '50%',
        border: '2px solid rgba(255,255,255,0.3)', borderTopColor: '#fff',
        animation: 'spin 0.8s linear infinite', marginRight: '6px', display: 'inline-block',
    },
    deployError: {
        margin: '10px 18px 0', padding: '8px 12px', background: 'rgba(220,38,38,0.06)',
        border: '1px solid rgba(220,38,38,0.25)', color: '#b91c1c',
        fontSize: '12px', borderRadius: '8px',
    },
    canvasWrap: {
        flex: '1 1 auto', minHeight: 0, position: 'relative',
        background: '#f8fafc',
    },
    canvasHintPanel: {
        padding: '6px 12px', background: 'rgba(15,23,42,0.6)',
        color: '#f8fafc', fontSize: '11.5px', fontWeight: 550,
        borderRadius: '999px', border: 'none',
        boxShadow: '0 2px 6px rgba(15,23,42,0.15)', pointerEvents: 'none',
    },
    backBtn: {
        display: 'inline-flex', alignItems: 'center', gap: '6px',
        padding: '6px 12px', fontSize: '12px', fontWeight: 600,
        color: '#0f172a', background: '#f8fafc',
        border: '1px solid var(--color-border, #dededd)',
        borderRadius: '8px', cursor: 'pointer', flexShrink: 0,
    },
    configHeaderSub: {
        marginTop: '2px', fontSize: '11.5px', color: '#64748b', lineHeight: 1.45,
    },
    configFullScroll: {
        flex: '1 1 auto', minHeight: 0, overflowY: 'auto',
        padding: '16px 20px 24px', background: '#ffffff',
    },
    canvasToolbar: {
        display: 'flex', gap: '6px', padding: '6px 8px',
        background: '#ffffff', border: '1px solid var(--color-border, #dededd)',
        borderRadius: '10px', boxShadow: '0 2px 8px rgba(15,23,42,0.06)',
    },
    toolbarBtn: {
        padding: '6px 12px', fontSize: '12px', fontWeight: 600,
        color: '#0f172a', background: '#f8fafc',
        border: '1px solid var(--color-border, #dededd)',
        borderRadius: '8px', cursor: 'pointer',
    },
    addMenu: {
        position: 'absolute', top: '100%', right: 0, marginTop: '6px',
        background: '#ffffff', border: '1px solid var(--color-border, #dededd)',
        borderRadius: '10px', boxShadow: '0 8px 24px rgba(15,23,42,0.12)',
        minWidth: '180px', zIndex: 20, overflow: 'hidden',
    },
    addMenuItem: {
        display: 'block', width: '100%', textAlign: 'left', padding: '8px 12px',
        background: 'transparent', border: 'none',
        fontSize: '12.5px', color: '#0f172a', cursor: 'pointer',
    },
    configEmpty: {
        padding: '32px 16px', textAlign: 'center', color: '#94a3b8',
        fontSize: '12.5px', lineHeight: 1.6, background: '#f8fafc',
        border: '1px dashed #cbd5e1', borderRadius: '12px',
    },
    formStack: { display: 'flex', flexDirection: 'column', gap: '12px' },
    readOnly: {
        padding: '18px', background: '#f8fafc',
        border: '1px solid var(--color-border, #dededd)', borderRadius: '10px',
    },
    readOnlyTitle: { fontSize: '13px', fontWeight: 700, color: '#0f172a', marginBottom: '4px' },
    readOnlyBody: { fontSize: '12px', color: '#64748b' },
};

export default WorkflowPreview;
