// SPDX-License-Identifier: Apache-2.0
import { create } from 'zustand';
import { temporal } from 'zundo';
import { applyNodeChanges, applyEdgeChanges } from '@xyflow/react';
import { findDuplicate, makeId } from '../utils/makeId';
import { newCase } from '../features/workflows/editor/conditions/factories';
import { KB_MODE_NONE } from '../components/common/KnowledgeSection';
import { DEFAULT_NODE_MODEL, DEFAULT_NODE_PROVIDER } from '../config/models';

// Shared retention cap for in-memory run telemetry (executionLogs + Debug
// Log rows). Long-running loops can emit thousands of events.
const MAX_RUN_ENTRIES = 1000;

// Immutable append-with-cap. When the array would exceed `cap`, oldest
// entries are evicted from the head. `onEvict` (optional) receives the
// evicted slice so callers can reconcile auxiliary indexes.
function pushCapped(arr, item, cap, onEvict) {
    if (arr.length < cap) return [...arr, item];
    const evicted = arr.slice(0, arr.length - cap + 1);
    if (onEvict) onEvict(evicted);
    return [...arr.slice(arr.length - cap + 1), item];
}

// Canonical empty Debug Log run-context. Used for the initial state,
// `clearRunContext`, and the chained reset in `clearExecutionState`.
//
// `runHistory` accumulates PRIOR runs from the same chat/thread so the Debug
// Log can show every run the user triggered in this session (newest first).
// The top-level fields (runId, rows, status, …) always describe the CURRENT /
// most-recent run; when a new run starts, `beginRunContext` snapshots the
// current run into `runHistory` before resetting. A brand-new chat wipes both.
function createEmptyRunContext() {
    return {
        runId: null,
        startedAt: null,
        status: 'idle',
        currentInput: '',
        finalOutput: '',
        executionTrace: [],
        loopContext: null,
        rows: [],
        rowIdByNode: {},
        runHistory: [],
    };
}

// Snapshot the CURRENT run's telemetry into a self-contained archive entry
// (used when a new run starts or the log is inspected). Returns null when the
// current run never produced anything worth keeping.
function snapshotCurrentRun(ctx) {
    if (!ctx || !ctx.runId || (ctx.rows || []).length === 0) return null;
    return {
        runId: ctx.runId,
        startedAt: ctx.startedAt,
        status: ctx.status,
        currentInput: ctx.currentInput,
        finalOutput: ctx.finalOutput,
        executionTrace: ctx.executionTrace,
        rows: ctx.rows,
    };
}

// Total rows retained across the whole archive so a session with many runs
// can never grow unbounded. When the newest run alone would exceed this it is
// kept in full (single-run integrity wins); older archived runs are evicted
// oldest-first to stay within budget.
const MAX_ARCHIVED_ROWS = 5000;

function capRunHistory(history) {
    let total = history.reduce((n, r) => n + (r.rows ? r.rows.length : 0), 0);
    if (total <= MAX_ARCHIVED_ROWS) return history;
    // history is newest-first; drop from the tail (oldest) until under budget,
    // but always keep at least the newest archived run.
    const kept = history.slice();
    while (kept.length > 1 && total > MAX_ARCHIVED_ROWS) {
        const dropped = kept.pop();
        total -= dropped.rows ? dropped.rows.length : 0;
    }
    return kept;
}

// Freeze a run: mark it 'stopped', flip any still-open node rows from
// 'running' to 'stopped' (no dangling spinners) and drop the open-row index.
// Shared by the stop path so the running→stopped mapping lives in one place.
function stopOpenRows(ctx) {
    return {
        ...ctx,
        status: 'stopped',
        rows: ctx.rows.map((r) => (
            r.status === 'running' ? { ...r, status: 'stopped' } : r
        )),
        rowIdByNode: {},
    };
}

// Default template nodes
const defaultNodes = [
    {
        id: 'start-default',
        type: 'start',
        position: { x: 100, y: 200 },
        data: { label: 'Start' },
    },
    {
        id: 'agent-default',
        type: 'agent',
        position: { x: 300, y: 200 },
        data: {
            name: '',
            instructions: '',
            provider: DEFAULT_NODE_PROVIDER,
            apiKey: '',
            modelName: DEFAULT_NODE_MODEL,
            temperature: 0.7,
            maxTokens: 2048,
            topP: 1.0,
            baseUrl: '',
        },
    },
    {
        id: 'end-default',
        type: 'end',
        position: { x: 520, y: 200 },
        data: { label: 'End' },
    },
];

const defaultEdges = [
    {
        id: 'edge-start-agent',
        source: 'start-default',
        target: 'agent-default',
        // Use 'ai-edge' to match the custom edge type registered in Canvas.jsx.
        // Canvas.jsx also normalises all edges to 'ai-edge' via renderedEdges,
        // but setting it here keeps the store consistent.
        type: 'ai-edge',
    },
    {
        id: 'edge-agent-end',
        source: 'agent-default',
        target: 'end-default',
        type: 'ai-edge',
    },
];

function createSessionWorkflowId() {
    return `wf_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function createWorkflowNodeId(type) {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
        return `${type}-${crypto.randomUUID()}`;
    }
    return `${type}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function createWorkflowEdgeId(suffix = '') {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
        return `edge-${crypto.randomUUID()}${suffix}`;
    }
    return `edge-${Date.now()}-${Math.random().toString(36).slice(2, 8)}${suffix}`;
}

// Generate a stable unique workflow ID once per app load (until set by dashboard id)
const _sessionWorkflowId = createSessionWorkflowId();

function buildAdjacency(nodes, edges) {
    const outgoing = new Map();
    const incoming = new Map();

    nodes.forEach((node) => {
        outgoing.set(node.id, []);
        incoming.set(node.id, []);
    });

    edges.forEach((edge) => {
        if (outgoing.has(edge.source)) outgoing.get(edge.source).push(edge);
        if (incoming.has(edge.target)) incoming.get(edge.target).push(edge);
    });

    return { outgoing, incoming };
}

function reachableFrom(startId, outgoing) {
    const visited = new Set();
    const stack = [startId];

    while (stack.length > 0) {
        const current = stack.pop();
        if (visited.has(current)) continue;
        visited.add(current);

        const edges = outgoing.get(current) || [];
        edges.forEach((edge) => {
            if (!visited.has(edge.target)) stack.push(edge.target);
        });
    }

    return visited;
}

function canReachEnd(endId, incoming) {
    const visited = new Set();
    const stack = [endId];

    while (stack.length > 0) {
        const current = stack.pop();
        if (visited.has(current)) continue;
        visited.add(current);

        const edges = incoming.get(current) || [];
        edges.forEach((edge) => {
            if (!visited.has(edge.source)) stack.push(edge.source);
        });
    }

    return visited;
}

// Returns the set of node IDs that participate in *any* path between Start
// and End. Anything else on the canvas is considered an "orphan" that the
// user dropped but never wired up — those nodes should be ignored both
// when validating and when sending the workflow to the backend.
function getConnectedNodeIds(nodes, edges) {
    const startNodes = nodes.filter((n) => n.type === 'start');
    const endNodes = nodes.filter((n) => n.type === 'end');
    if (startNodes.length !== 1 || endNodes.length !== 1) {
        return new Set(nodes.map((n) => n.id));
    }
    const { outgoing, incoming } = buildAdjacency(nodes, edges);
    const fromStart = reachableFrom(startNodes[0].id, outgoing);
    const toEnd = canReachEnd(endNodes[0].id, incoming);
    const connected = new Set();
    nodes.forEach((node) => {
        if (fromStart.has(node.id) && toEnd.has(node.id)) {
            connected.add(node.id);
        }
    });
    return connected;
}

function pruneToConnectedSubgraph(nodes, edges) {
    const ids = getConnectedNodeIds(nodes, edges);
    return {
        nodes: nodes.filter((n) => ids.has(n.id)),
        edges: edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
    };
}

function hasDirectedCycle(nodes, outgoing) {
    const color = new Map(); // 0=unvisited, 1=visiting, 2=done
    nodes.forEach((node) => color.set(node.id, 0));

    const visit = (nodeId) => {
        color.set(nodeId, 1);
        const edges = outgoing.get(nodeId) || [];

        for (const edge of edges) {
            const next = edge.target;
            const nextColor = color.get(next) ?? 0;

            if (nextColor === 1) return true;
            if (nextColor === 0 && visit(next)) return true;
        }

        color.set(nodeId, 2);
        return false;
    };

    for (const node of nodes) {
        if ((color.get(node.id) ?? 0) === 0 && visit(node.id)) {
            return true;
        }
    }

    return false;
}

// A cycle is "illegal" unless it closes on a loop node through that loop's
// 'body' handle. The loop engine drives those back-edges itself
// (stop_at={loop_id}), so they don't represent unbounded recursion.
// Reuses the prebuilt outgoing map from buildAdjacency by wrapping it with
// a body-handle skip predicate, avoiding a second full pass over edges.
function hasIllegalCycle(nodes, outgoing) {
    const loopNodeIds = new Set(
        nodes.filter((n) => n.type === 'loop').map((n) => n.id)
    );
    const filtered = new Map();
    for (const [nodeId, edges] of outgoing.entries()) {
        if (loopNodeIds.has(nodeId)) {
            filtered.set(nodeId, edges.filter((e) => (e.sourceHandle || '') !== 'body'));
        } else {
            filtered.set(nodeId, edges);
        }
    }
    return hasDirectedCycle(nodes, filtered);
}

// True iff some path through the loop's 'body' handle reaches back to
// loopId without first crossing endId.
function loopBodyClosesOnNode(loopId, outEdges, outgoing, endId) {
    const bodyStarts = outEdges
        .filter((e) => (e.sourceHandle || '') === 'body')
        .map((e) => e.target);

    const visited = new Set();
    const stack = [...bodyStarts];
    while (stack.length) {
        const cur = stack.pop();
        if (visited.has(cur)) continue;
        visited.add(cur);
        if (cur === loopId) return true;
        if (cur === endId) continue;
        for (const e of outgoing.get(cur) || []) {
            if (!visited.has(e.target)) stack.push(e.target);
        }
    }
    return false;
}

const useWorkflowStore = create(temporal((set, get) => ({
    // React Flow Data
    nodes: defaultNodes,
    edges: defaultEdges,

    // UI State
    selectedNodeId: null,
    hoveredEdgeId: null,

    // Execution State
    isExecuting: false,
    executionResult: null,
    executionError: null,
    executionLogs: [],
    currentAgent: null,
    activeNodeIds: [],

    // Debug Log run context — normalised timeline view of the current run
    // (status, ordered rows with stable identity, finalised execution_trace).
    // The `rowIdByNode` map tracks the latest open `running` row per nodeId
    // so the matching close event can update it in place. There is no
    // system-variables concept in this engine, so no variables dict here.
    runContext: createEmptyRunContext(),

    // Stable workflow ID for document isolation (unique per session/workflow)
    workflowId: _sessionWorkflowId,
    setWorkflowId: (workflowId) => set({
        workflowId: workflowId || createSessionWorkflowId(),
    }),

    // Workflow name (set when opening a workflow from the dashboard)
    workflowName: 'New workflow',
    setWorkflowName: (name) => set({ workflowName: name }),

    // Workflow-level KB blob. The engine falls back to this for any agent
    // node whose own `data.knowledge.mode === 'none'`.
    workflowKnowledge: { mode: KB_MODE_NONE },
    setWorkflowKnowledge: (knowledge) => set({
        workflowKnowledge: knowledge || { mode: KB_MODE_NONE },
    }),

    // Chat-panel "Run settings" — workflow-wide subagent (swarm) opt-in
    // applied to the NEXT run from this chat. Resolution at the engine:
    //   1. Per-node `disable_subagents=true` pin → swarm OFF for that node
    //   2. Per-node `enable_subagents=true`  pin → swarm ON for that node
    //                                              (overrides run-level OFF)
    //   3. This run-level flag                   → applies to all
    //                                              otherwise-unpinned nodes
    //   4. No client signal                      → engine default (ON)
    //
    // Default is `false` (disabled) per product call: enterprise users opt
    // INTO delegation explicitly, never get surprised by an unexpected
    // swarm spend.
    runSubagentsEnabled: false,
    setRunSubagentsEnabled: (enabled) => set({
        runSubagentsEnabled: Boolean(enabled),
    }),

    // True while the user is actually on the chat (preview) surface for
    // this workflow — the bell uses it to suppress toast pop-ups for
    // executions whose results are already being streamed into the chat.
    isViewingChat: false,
    setViewingChat: (v) => set({ isViewingChat: !!v }),

    // Latest chat thread the user has interacted with on this workflow.
    // Set by ChatPanel; read by ConfigPanel's Loop config to fetch the
    // upstream node's last output for the connection-aware list picker.
    activeThreadId: '',
    setActiveThreadId: (tid) => set((state) => (
        state.activeThreadId === (tid || '') ? state : { activeThreadId: tid || '' }
    )),

    // ----- Preview-mode chat state ---------------------------------------
    // These used to live as local useState inside ChatPanel. Lifting them
    // into the store means switching mode (preview → edit) or navigating
    // to the dashboard no longer drops the chat history, the in-flight
    // streamed assistant reply, or the HITL approval card. ChatPanel is
    // also kept mounted across mode toggles (see App.jsx) so SSE handlers
    // continue to write into these fields while the user is inspecting a
    // node config or browsing the dashboard.
    chatMessages: [],
    chatStreamingContent: '',
    chatStreamingAgent: '',
    chatThreadId: '',
    chatHitlRequest: null,
    chatHitlRedirectText: '',
    chatFailureSnapshot: null,   // { threadId, nodeId, agent, error, errorType, completedNodes, lastInput }
    // Tracks which workflow id the in-store chat state belongs to. When the
    // user opens a different workflow we wipe the chat slice so threads from
    // workflow A don't bleed into workflow B's preview pane.
    chatOwnerWorkflowId: null,

    // Each setter short-circuits when the value is unchanged so SSE token
    // streams don't notify every selector subscriber 60×/sec on no-ops
    // (e.g. the same agent name being re-set as each token arrives, or an
    // empty-string clear running twice in a row).
    setChatMessages: (updater) => set((state) => {
        const raw = typeof updater === 'function' ? updater(state.chatMessages) : updater;
        const next = Array.isArray(raw) ? raw : [];
        return next === state.chatMessages ? state : { chatMessages: next };
    }),
    setChatStreamingContent: (updater) => set((state) => {
        const raw = typeof updater === 'function' ? updater(state.chatStreamingContent) : updater;
        const next = raw || '';
        return next === state.chatStreamingContent ? state : { chatStreamingContent: next };
    }),
    setChatStreamingAgent: (agent) => set((state) => {
        const next = agent || '';
        return next === state.chatStreamingAgent ? state : { chatStreamingAgent: next };
    }),
    setChatThreadId: (tid) => set((state) => {
        const next = tid || '';
        return next === state.chatThreadId ? state : { chatThreadId: next };
    }),
    setChatHitlRequest: (req) => set((state) => {
        const next = req || null;
        return next === state.chatHitlRequest ? state : { chatHitlRequest: next };
    }),
    setChatHitlRedirectText: (text) => set((state) => {
        const next = text || '';
        return next === state.chatHitlRedirectText ? state : { chatHitlRedirectText: next };
    }),
    setChatFailureSnapshot: (req) => set((state) => {
        const next = req || null;
        return next === state.chatFailureSnapshot ? state : { chatFailureSnapshot: next };
    }),

    // Called by App.handleOpenWorkflow when a new workflow is opened.
    // Clears stale chat from the previously-opened workflow so the preview
    // panel doesn't briefly flash the wrong thread before history reloads.
    resetChatStateForWorkflow: (workflowId) => set((state) => {
        if (state.chatOwnerWorkflowId === workflowId) return state;
        return {
            chatOwnerWorkflowId: workflowId,
            chatMessages: [],
            chatStreamingContent: '',
            chatStreamingAgent: '',
            chatThreadId: '',
            chatHitlRequest: null,
            chatHitlRedirectText: '',
            chatFailureSnapshot: null,
        };
    }),

    // Actions for Nodes
    addNode: (type, position) => {
        const id = createWorkflowNodeId(type);
        const newNode = {
            id,
            type,
            position,
            data: getDefaultNodeData(type),
        };
        set((state) => ({
            nodes: [...state.nodes, newNode],
        }));
        return id;
    },

    // Insert a node onto an existing edge, splitting it into two edges.
    // Atomically: removes the old edge, creates the new node, and wires
    // source → newNode → target in a single set() call.
    insertNodeOnEdge: (edgeId, nodeType, position) => {
        const state = get();
        const edge = state.edges.find((e) => e.id === edgeId);
        if (!edge) return null;

        const newNodeId = createWorkflowNodeId(nodeType);
        const newNode = {
            id: newNodeId,
            type: nodeType,
            position,
            data: getDefaultNodeData(nodeType),
        };

        // Edge from original source → new node (preserve sourceHandle)
        const edgeToNew = {
            id: createWorkflowEdgeId('-a'),
            source: edge.source,
            sourceHandle: edge.sourceHandle || null,
            target: newNodeId,
            targetHandle: 'target',
            type: 'ai-edge',
        };

        // Edge from new node → original target (preserve targetHandle)
        const edgeFromNew = {
            id: createWorkflowEdgeId('-b'),
            source: newNodeId,
            sourceHandle: 'source',
            target: edge.target,
            targetHandle: edge.targetHandle || null,
            type: 'ai-edge',
        };

        set({
            nodes: [...state.nodes, newNode],
            edges: [
                ...state.edges.filter((e) => e.id !== edgeId),
                edgeToNew,
                edgeFromNew,
            ],
        });

        return newNodeId;
    },

    updateNodeData: (nodeId, newData) => {
        set((state) => {
            const target = state.nodes.find((n) => n.id === nodeId);
            if (!target) return state;
            // Shallow-equal short-circuit: when every newData key matches
            // the existing value by reference, skip the set() to avoid
            // re-renders on no-op updates (typed-and-restored keystrokes,
            // unchanged config-panel submits, etc.).
            const unchanged = Object.keys(newData).every(
                (k) => target.data[k] === newData[k]
            );
            if (unchanged) return state;
            return {
                nodes: state.nodes.map((node) =>
                    node.id === nodeId
                        ? { ...node, data: { ...node.data, ...newData } }
                        : node
                ),
            };
        });
    },

    removeNode: (nodeId) => {
        set((state) => ({
            nodes: state.nodes.filter((node) => node.id !== nodeId),
            edges: state.edges.filter(
                (edge) => edge.source !== nodeId && edge.target !== nodeId
            ),
            selectedNodeId: state.selectedNodeId === nodeId ? null : state.selectedNodeId,
        }));
    },

    setNodes: (nodes) => set({ nodes }),

    onNodesChange: (changes) => {
        set((state) => ({
            nodes: applyNodeChanges(changes, state.nodes),
        }));
    },

    // Actions for Edges
    addEdge: (edge) => {
        // Use 'ai-edge' to match the custom edge type; Canvas.jsx also
        // normalises all edges to 'ai-edge' via renderedEdges, but setting
        // it here keeps the store consistent and avoids a flash of the
        // default edge style before the memoised override kicks in.
        const newEdge = {
            ...edge,
            id: createWorkflowEdgeId(),
            type: 'ai-edge',
        };

        set((state) => ({
            edges: [...state.edges, newEdge],
        }));
    },

    removeEdge: (edgeId) => {
        set((state) => ({
            edges: state.edges.filter((edge) => edge.id !== edgeId),
        }));
    },

    setEdges: (edges) => set({ edges }),

    onEdgesChange: (changes) => {
        set((state) => ({
            edges: applyEdgeChanges(changes, state.edges),
        }));
    },

    onConnect: (connection) => {
        get().addEdge(connection);
    },

    // Actions for Selection
    setSelectedNode: (nodeId) => set({ selectedNodeId: nodeId }),
    clearSelection: () => set({ selectedNodeId: null }),

    // Edge hover tracking (used by AiEdge to show/hide the "+" insert button)
    setHoveredEdgeId: (id) => set({ hoveredEdgeId: id }),

    // Actions for Execution
    setExecuting: (status) => set({ isExecuting: status }),
    setExecutionResult: (result) => set({ executionResult: result }),
    setExecutionError: (error) => set({ executionError: error }),
    setCurrentAgent: (agent) => set({ currentAgent: agent }),

    addExecutionLog: (log) => {
        // Stamp `ts` ONCE at creation so it's frozen into the immutable log
        // entry. Downstream consumers (e.g. the sub-agent live elapsed timer
        // in ChatPanel's buildAgentTimeline) must read this instead of
        // calling Date.now() during rebuilds — otherwise every rebuild would
        // reset running timers. Only set when absent so replays keep theirs.
        const stamped = (log && log.ts == null) ? { ...log, ts: Date.now() } : log;
        set((state) => ({
            executionLogs: pushCapped(state.executionLogs, stamped, MAX_RUN_ENTRIES),
        }));
    },

    clearExecutionLogs: () => set({ executionLogs: [] }),

    // Debug Log run-context actions. `appendRunEvent` either pushes a new
    // row or, when a close event (done|error) arrives for a nodeId with an
    // open running row, updates that row in place — keeping the timeline
    // free of duplicate start/finish pairs.
    beginRunContext: (seed = {}) => {
        const runId = seed.runId || makeId('run');
        set((state) => {
            const prev = state.runContext;
            // Preserve the PRIOR run: snapshot it into runHistory (newest
            // first) so the user can scroll back and inspect earlier runs
            // in the same chat. A stopped/running prior run is archived as
            // 'stopped' so its status is honest. New-chat still wipes via
            // clearExecutionState → createEmptyRunContext.
            const snap = snapshotCurrentRun(prev);
            let history = prev.runHistory || [];
            if (snap) {
                if (snap.status === 'running') snap.status = 'stopped';
                history = capRunHistory([snap, ...history]);
            }
            return {
                runContext: {
                    ...createEmptyRunContext(),
                    runHistory: history,
                    runId,
                    startedAt: seed.startedAt || new Date().toISOString(),
                    status: 'running',
                    currentInput: seed.currentInput || '',
                },
            };
        });
        return runId;
    },

    appendRunEvent: (event) => {
        const incoming = {
            id: (event && event.id) || makeId('row'),
            ts: (event && event.ts) || new Date().toISOString(),
            group: event.group || null,
            nodeId: event.nodeId || null,
            nodeLabel: event.nodeLabel || event.nodeId || '',
            title: event.title || '',
            detail: event.detail || '',
            status: event.status || 'done',
            kind: event.kind || null,
            kbHint: event.kbHint || null,
            generatedFiles: Array.isArray(event.generatedFiles) ? event.generatedFiles : null,
            raw: event.raw || null,
        };
        set((state) => {
            const ctx = state.runContext;
            const openRowId = incoming.nodeId ? ctx.rowIdByNode[incoming.nodeId] : null;
            const isClose = openRowId && (incoming.status === 'done' || incoming.status === 'error');
            if (isClose) {
                const idx = ctx.rows.findIndex((r) => r.id === openRowId);
                if (idx === -1) {
                    // Open id stale (likely evicted by the row cap). Fall through to append.
                } else {
                    const rows = ctx.rows.slice();
                    rows[idx] = {
                        ...rows[idx],
                        status: incoming.status,
                        // Keep original title; let the close event refine
                        // detail/raw/files payload.
                        detail: incoming.detail || rows[idx].detail,
                        raw: incoming.raw || rows[idx].raw,
                        generatedFiles: incoming.generatedFiles || rows[idx].generatedFiles,
                        tsClosed: incoming.ts,
                    };
                    const rowIdByNode = { ...ctx.rowIdByNode };
                    delete rowIdByNode[incoming.nodeId];
                    return { runContext: { ...ctx, rows, rowIdByNode } };
                }
            }
            // Append path. Reconcile rowIdByNode when the cap evicts a row
            // whose id is still tracked as "open" — otherwise the index
            // would point at a row that no longer exists.
            let rowIdByNode = ctx.rowIdByNode;
            const rows = pushCapped(ctx.rows, incoming, MAX_RUN_ENTRIES, (evicted) => {
                let cloned = null;
                for (const r of evicted) {
                    if (r.nodeId && rowIdByNode[r.nodeId] === r.id) {
                        if (!cloned) { cloned = { ...rowIdByNode }; }
                        delete cloned[r.nodeId];
                    }
                }
                if (cloned) rowIdByNode = cloned;
            });
            if (incoming.nodeId && incoming.status === 'running') {
                rowIdByNode = { ...rowIdByNode, [incoming.nodeId]: incoming.id };
            }
            return { runContext: { ...ctx, rows, rowIdByNode } };
        });
    },

    setRunStatus: (status) => set((state) => (
        state.runContext.status === status
            ? state
            : { runContext: { ...state.runContext, status } }
    )),

    setRunContextFromComplete: (finalPayload) => {
        // The backend ALWAYS emits `complete` at the end of a run, even
        // after an `error`. Preserve a prior 'error' status so the Debug
        // Log doesn't show a misleading "SUCCESS" row under the red one.
        // On error we suppress finalOutput because `data.output` is the
        // error string itself.
        //
        // We deliberately do NOT overwrite `currentInput` here anymore.
        // Previously we mirrored the model's final output into
        // `currentInput`, which caused the Debug Log → Session Context
        // panel to show the SAME text three times (Current Input, Final
        // Output, and the sole Execution Trace step for single-agent
        // flows). Keep `currentInput` pinned to whatever the user
        // actually typed / seeded via ``beginRunContext``.
        const trace = Array.isArray(finalPayload?.execution_trace)
            ? finalPayload.execution_trace
            : [];
        const rawOutput = typeof finalPayload?.output === 'string'
            ? finalPayload.output
            : '';
        set((state) => {
            const errored = state.runContext.status === 'error' || !!finalPayload?.hitl_rejected;
            return {
                runContext: {
                    ...state.runContext,
                    status: errored ? 'error' : 'done',
                    executionTrace: trace,
                    finalOutput: errored ? '' : rawOutput,
                    // currentInput preserved from beginRunContext seed.
                },
            };
        });
    },

    clearRunContext: () => set({ runContext: createEmptyRunContext() }),

    // Active Node Tracking
    setNodeActive: (nodeId) => {
        set((state) => (
            state.activeNodeIds.includes(nodeId)
                ? state
                : { activeNodeIds: [...state.activeNodeIds, nodeId] }
        ));
    },
    clearNodeActive: (nodeId) => {
        set((state) => ({
            activeNodeIds: state.activeNodeIds.filter(id => id !== nodeId)
        }));
    },
    clearAllActiveNodes: () => set({ activeNodeIds: [] }),

    // Transient per-Loop-node progress driven by SSE loop_* events. Kept
    // off `nodes` so it never bleeds into the saved workflow definition.
    // Shape: { [nodeId]: { running: bool, index: number, total?: number, mode: string } }
    loopProgress: {},
    setLoopProgress: (nodeId, partial) => {
        set((state) => ({
            loopProgress: {
                ...state.loopProgress,
                [nodeId]: { ...(state.loopProgress[nodeId] || {}), ...partial },
            },
        }));
    },
    clearLoopProgress: (nodeId) => {
        set((state) => {
            const next = { ...state.loopProgress };
            delete next[nodeId];
            return { loopProgress: next };
        });
    },

    clearExecutionState: () => set({
        isExecuting: false,
        executionResult: null,
        executionError: null,
        executionLogs: [],
        currentAgent: null,
        activeNodeIds: [],
        loopProgress: {},
        // Wipe the debug-log timeline so a new chat / new run starts clean.
        runContext: createEmptyRunContext(),
    }),

    // Like clearExecutionState but PRESERVES the Debug Log timeline. Used when
    // a run is stopped mid-flight: the transient streaming/live-node UI is
    // reset, but runContext (rows + runHistory) is kept and the current run is
    // marked 'stopped' so the operator can review exactly what ran before the
    // interruption and re-run afterwards.
    stopRunPreservingLog: () => set((state) => {
        const ctx = state.runContext;
        const stoppedCtx = ctx.runId ? stopOpenRows(ctx) : ctx;
        return {
            isExecuting: false,
            executionResult: null,
            executionError: null,
            executionLogs: [],
            currentAgent: null,
            activeNodeIds: [],
            loopProgress: {},
            runContext: stoppedCtx,
        };
    }),

    // Utility Actions
    getWorkflowForExecution: () => {
        const state = get();

        // Drop orphan nodes (e.g. an "Existing Workflow" the user dropped
        // on the canvas but never connected). They would otherwise trip
        // the backend's validator and fail the whole run.
        const { nodes, edges } = pruneToConnectedSubgraph(state.nodes, state.edges);

        const exportedNodes = nodes.map((node) => {
            if (node.type === 'start' || node.type === 'end') {
                return { id: node.id, type: node.type };
            }

            if (node.type === 'condition') {
                return {
                    id: node.id,
                    type: node.type,
                    cases: node.data.cases || [],
                };
            }

            if (node.type === 'subflow') {
                // Hand the backend exactly what `_run_subflow` in native_engine.py
                // needs to dispatch into the referenced agent / workflow.
                return {
                    id: node.id,
                    type: node.type,
                    kind: node.data.kind || 'agent',
                    refId: node.data.refId || '',
                    refName: node.data.refName || '',
                };
            }

            if (node.type === 'loop') {
                // Only forward evaluator keys when opted in, so default
                // values can evolve without rewriting saved workflows.
                const evaluatorPayload = node.data.useLlmEvaluator
                    ? {
                        useLlmEvaluator: true,
                        confidenceThreshold: node.data.confidenceThreshold ?? 0.85,
                        similarityThreshold: node.data.similarityThreshold ?? 0.95,
                        regressionDelta:     node.data.regressionDelta ?? 0.05,
                        stopMode:            (node.data.stopMode || 'adaptive'),
                        evaluatorTask:       node.data.evaluatorTask || '',
                        evaluatorRubric:     node.data.evaluatorRubric || '',
                        ...(node.data.evaluatorModelName
                            ? { evaluatorLlmConfig: { model_name: node.data.evaluatorModelName } }
                            : {}),
                    }
                    : {};

                // Only the optional display name is still forwarded; the
                // verifier-timeout / token-budget / wall-clock / memory
                // fields have been removed from the Loop UI entirely.
                const goalPayload = {
                    ...(node.data.name ? { name: node.data.name } : {}),
                };

                return {
                    id: node.id,
                    type: node.type,
                    mode: node.data.mode || 'for_each',
                    itemsExpression: node.data.itemsExpression || 'input.items',
                    count: node.data.count ?? 3,
                    cases: node.data.cases || [],
                    maxIterations: node.data.maxIterations ?? 5,
                    iteratorVar: node.data.iteratorVar || 'item',
                    ...evaluatorPayload,
                    ...goalPayload,
                };
            }

            return {
                id: node.id,
                type: node.type,
                name: node.data.name,
                instructions: node.data.instructions,
                hitlMode: node.data.hitlMode || 'off',
                llm_config: {
                    provider: 'google',
                    api_key: node.data.apiKey || '',
                    model_name: node.data.modelName || '',
                    temperature: node.data.temperature,
                    max_tokens: node.data.maxTokens,
                    top_p: node.data.topP,
                    base_url: node.data.baseUrl || '',
                },
                tools: node.data.tools || [],
                skills: node.data.skills || [],
                knowledge: node.data.knowledge || { mode: KB_MODE_NONE },
                // Per-node subagent pins. The backend gate at
                // native_engine.py `_node_pinned_on/off` reads these
                // exact field names; omitting them makes the toggle
                // silently a no-op regardless of what the UI shows.
                // Only forward when explicitly set so we don't paint
                // legacy saved workflows with false defaults.
                ...(node.data.enable_subagents  ? { enable_subagents:  true } : {}),
                ...(node.data.disable_subagents ? { disable_subagents: true } : {}),
            };
        });

        const exportedEdges = edges.map((edge) => ({
            source: edge.source,
            target: edge.target,
            sourceHandle: edge.sourceHandle || null,
        }));

        return {
            nodes: exportedNodes,
            edges: exportedEdges,
            knowledge: state.workflowKnowledge || { mode: KB_MODE_NONE },
        };
    },


    resetWorkflow: () => set({
        nodes: [],
        edges: [],
        selectedNodeId: null,
        isExecuting: false,
        executionResult: null,
        executionError: null,
        executionLogs: [],
        currentAgent: null,
        workflowId: createSessionWorkflowId(),
        workflowKnowledge: { mode: KB_MODE_NONE },
        // Wipe the preview-mode chat slice too — the workflow being closed
        // is gone, so its in-memory chat shouldn't haunt the next one the
        // user opens.
        chatMessages: [],
        chatStreamingContent: '',
        chatStreamingAgent: '',
        chatThreadId: '',
        chatHitlRequest: null,
        chatHitlRedirectText: '',
        chatOwnerWorkflowId: null,
    }),

    // Computed/Derived
    getSelectedNode: () => {
        const { nodes, selectedNodeId } = get();
        return nodes.find((node) => node.id === selectedNodeId) || null;
    },

    isWorkflowValid: () => {
        const state = get();

        // Validate only the connected Start→End subgraph. Orphan nodes
        // the user left lying on the canvas should not block execution —
        // getWorkflowForExecution strips them out of the payload, so
        // validation must agree.
        const { nodes, edges } = pruneToConnectedSubgraph(state.nodes, state.edges);
        const nodeById = new Map(nodes.map((node) => [node.id, node]));

        const startNodes = nodes.filter((n) => n.type === 'start');
        const endNodes = nodes.filter((n) => n.type === 'end');
        const hasStart = startNodes.length > 0;
        const hasEnd = endNodes.length > 0;
        const hasExecutableNode = nodes.some((n) => ['agent', 'subflow', 'evaluation_gate'].includes(n.type));

        if (!hasStart || !hasEnd || !hasExecutableNode) {
            return { valid: false, error: 'Workflow must have Start, End, and at least one executable node' };
        }

        if (startNodes.length !== 1) {
            return { valid: false, error: 'Workflow must have exactly one Start node' };
        }

        if (endNodes.length !== 1) {
            return { valid: false, error: 'Workflow must have exactly one End node' };
        }

        // Edge references must point to existing nodes
        const danglingEdge = edges.find(
            (edge) => !nodeById.has(edge.source) || !nodeById.has(edge.target)
        );
        if (danglingEdge) {
            return { valid: false, error: 'Workflow has an edge connected to a missing node' };
        }

        // Check all agents are configured
        const unconfiguredAgent = nodes.find(
            (n) => {
                if (n.type !== 'agent') return false;

                const hasName = !!n.data.name;
                const hasInstructions = !!n.data.instructions;
                if (!hasName || !hasInstructions) return true;

                return false;
            }
        );

        if (unconfiguredAgent) {
            return { valid: false, error: `Agent "${unconfiguredAgent.data.name || 'Unnamed'}" is not fully configured` };
        }

        // Subflow nodes must be linked to a saved agent or workflow before run.
        const unlinkedSubflow = nodes.find(
            (n) => n.type === 'subflow' && !n.data?.refId
        );
        if (unlinkedSubflow) {
            return {
                valid: false,
                error: 'An "Existing Asset" node is not linked to a workflow or agent yet'
            };
        }

        const { outgoing, incoming } = buildAdjacency(nodes, edges);
        const startId = startNodes[0].id;
        const endId = endNodes[0].id;

        // Basic directed-flow constraints
        if ((outgoing.get(startId) || []).length === 0) {
            return { valid: false, error: 'Start node must connect to at least one node' };
        }
        if ((incoming.get(endId) || []).length === 0) {
            return { valid: false, error: 'End node must have at least one incoming connection' };
        }

        // Condition branch completeness
        let conditionError = null;
        const badCondition = nodes.find((node) => {
            if (node.type !== 'condition') return false;
            const cases = node.data?.cases || [];
            if (cases.length === 0) {
                conditionError = 'Condition node has no cases defined.';
                return true;
            }
            // 'else' would shadow the real ELSE branch in condition_edges.
            if (cases.some((c) => c.id === 'else')) {
                conditionError = '"else" is a reserved case id. Rename the case.';
                return true;
            }
            const ids = cases.map((c) => c.id).filter(Boolean);
            const dup = findDuplicate(ids);
            if (dup) {
                conditionError = `Condition node has duplicate case id "${dup}".`;
                return true;
            }
            const emptyCase = cases.find((c) => {
                const rows = c.conditions || [];
                return rows.length === 0 || !rows.some((r) => r.field && r.operator);
            });
            if (emptyCase) {
                conditionError = `Case "${emptyCase.label || emptyCase.id}" has no configured condition rows.`;
                return true;
            }
            const availableHandles = new Set(
                (outgoing.get(node.id) || []).map((e) => e.sourceHandle || 'else')
            );
            if (!availableHandles.has('else')) {
                conditionError = 'Connect the ELSE handle of the condition node.';
                return true;
            }
            const missing = ids.find((id) => !availableHandles.has(id));
            if (missing) {
                conditionError = `Case handle "${missing}" is not connected to a downstream node.`;
                return true;
            }
            const idSet = new Set(ids);
            const unknown = [...availableHandles].find((h) => h !== 'else' && !idSet.has(h));
            if (unknown) {
                conditionError = `Edge references unknown case handle "${unknown}".`;
                return true;
            }
            return false;
        });
        if (badCondition) {
            return {
                valid: false,
                error: conditionError || 'Condition node branches are incomplete.',
            };
        }

        // Every non-end flow node should have at least one outgoing edge
        const deadEndNode = nodes.find(
            (node) => node.type !== 'end' && (outgoing.get(node.id) || []).length === 0
        );
        if (deadEndNode) {
            return {
                valid: false,
                error: `Node "${deadEndNode.data?.name || deadEndNode.data?.label || deadEndNode.id}" has no outgoing connection`
            };
        }

        // Every non-start flow node should have at least one incoming edge
        const noIncomingNode = nodes.find(
            (node) => node.type !== 'start' && (incoming.get(node.id) || []).length === 0
        );
        if (noIncomingNode) {
            return {
                valid: false,
                error: `Node "${noIncomingNode.data?.name || noIncomingNode.data?.label || noIncomingNode.id}" has no incoming connection`
            };
        }

        // Reachability checks
        const fromStart = reachableFrom(startId, outgoing);
        if (!fromStart.has(endId)) {
            return { valid: false, error: 'No valid path exists from Start to End' };
        }

        const unreachableNode = nodes.find((node) => !fromStart.has(node.id));
        if (unreachableNode) {
            return {
                valid: false,
                error: `Node "${unreachableNode.data?.name || unreachableNode.data?.label || unreachableNode.id}" is unreachable from Start`
            };
        }

        const toEnd = canReachEnd(endId, incoming);
        const cannotReachEndNode = nodes.find(
            (node) => node.type !== 'end' && !toEnd.has(node.id)
        );
        if (cannotReachEndNode) {
            return {
                valid: false,
                error: `Node "${cannotReachEndNode.data?.name || cannotReachEndNode.data?.label || cannotReachEndNode.id}" cannot reach End`
            };
        }

        // Loop nodes must have both a 'body' and 'exit' handle wired, and the
        // body subgraph must close back on the loop node so iteration can
        // restart. Body→loop back-edges are the only legal cycles.
        const badLoop = nodes.find((node) => {
            if (node.type !== 'loop') return false;
            const outEdges = outgoing.get(node.id) || [];
            const handles = new Set(outEdges.map((e) => e.sourceHandle || ''));
            if (!handles.has('body') || !handles.has('exit')) return true;
            return !loopBodyClosesOnNode(node.id, outEdges, outgoing, endId);
        });
        if (badLoop) {
            return {
                valid: false,
                error: `Loop node "${badLoop.data?.label || badLoop.id}" must have both a body and exit connection, and the body must loop back to it.`
            };
        }

        if (hasIllegalCycle(nodes, outgoing)) {
            return {
                valid: false,
                error: 'Workflow contains an unsupported cycle. Wrap repeated work in a Loop node.'
            };
        }

        return { valid: true, error: null };
    },
}), {
    // Only track nodes and edges for undo/redo — skip UI state,
    // execution state, hover state, etc.
    partialize: (state) => ({
        nodes: state.nodes,
        edges: state.edges,
    }),
    limit: 50,
}));

// Helper function to get default data for each node type
function getDefaultNodeData(type) {
    switch (type) {
        case 'start':
            return { label: 'Start' };
        case 'end':
            return { label: 'End' };
        case 'agent':
            return {
                name: 'Agent',
                instructions: '',
                provider: DEFAULT_NODE_PROVIDER,
                apiKey: '',
                modelName: DEFAULT_NODE_MODEL,
                temperature: 0.7,
                maxTokens: 2048,
                topP: 1.0,
                baseUrl: '',
                // Mirrors AgentEditor's default; shape is the backend KB_MODE_*
                // contract in backend/app/core/kb_retriever.py.
                knowledge: { mode: KB_MODE_NONE },
            };
        case 'condition':
            return {
                label: 'Condition',
                cases: [newCase('Case 1')],
            };
        case 'subflow':
            // Subflow nodes carry a pointer to an existing agent or workflow.
            // `kind` is 'agent' or 'workflow'; `refId` is the saved asset's id;
            // `refName` is cached locally so the canvas can render the label
            // without a round-trip to the API.
            return {
                kind: 'agent',
                refId: '',
                refName: '',
            };
        case 'loop':
            // Schema documented in backend/app/engine/native_engine.py::_run_loop.
            // The `useLlmEvaluator` block below is read by
            // backend/app/engine/loop_evaluator.py and only activates when
            // the user opts in via the Loop config panel — defaults are
            // declared here so new nodes don't carry undefined fields the
            // serializer would have to special-case.
            return {
                label: 'Loop',
                mode: 'for_each',
                itemsExpression: 'input.items',
                count: 3,
                cases: [],
                maxIterations: 5,
                iteratorVar: 'item',
                // --- LLM evaluator (off by default; legacy self-report wins) ---
                useLlmEvaluator: false,
                confidenceThreshold: 0.85,
                similarityThreshold: 0.95,
                regressionDelta: 0.05,
                stopMode: 'adaptive',   // 'adaptive' | 'fixed'
                evaluatorTask: '',
                evaluatorRubric: '',    // string = full prompt override; empty = use built-in rubric
                // Empty = backend picks the gateway default (keeps saved
                // workflows working without a re-save).
                evaluatorModelName: '',
                name: '',
            };
        case 'evaluation_gate':
            // Loop Engineering P2 in-graph judge gate. Backend dispatcher
            // lives in backend/app/engine/native_engine.py
            // ::_route_evaluation_gate. The default threshold matches the
            // proof-check llm_judge default in
            // backend/app/loop/proof.py::_llm_judge.
            return {
                label: 'Evaluation Gate',
                criteria: '',
                threshold: 0.7,
            };
        default:
            return {};
    }
}

export default useWorkflowStore;
