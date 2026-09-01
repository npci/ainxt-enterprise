// SPDX-License-Identifier: Apache-2.0
// Debug Log view — full-swap replacement for the chat-messages + composer
// inside the workflow ChatPanel. Renders the per-run timeline that the
// workflowStore.runContext slice accumulates from SSE events.
//
// Visual contract (single unified timeline, no tabs):
//   The view renders ONE phase-ordered scrollable list, partitioned by
//   row.kind into four sections so the reader can trace the run
//   end-to-end without seeing the same info twice:
//
//       INPUT      — the user prompt (one row)
//       EXECUTION  — Start → node events → End, in timestamp order
//       OUTPUT     — the final assistant response (one row)
//       METADATA   — Status pill + Tokens estimate
//
//   Each timeline row is ONE LINE by default:
//        ● {nodeLabel}              {HH:MM:SS}
//        small grey sub-line {title}
//   Status is conveyed only by the dot colour. Pills, kind-chips,
//   output snippets, View JSON button, KB hint, generated-files line —
//   ALL live INSIDE the expanded body, hidden until the row is clicked.
//   When a single node fires multiple SSE events (agent_start → tool_call
//   → tool_result → agent_complete), the events collapse under one parent
//   row whose status mirrors the worst sub-status. Expanding a node row
//   also surfaces the matching execution-trace step metadata (agent name,
//   char count, engine, tokens) and a second "View trace JSON" button —
//   the exact info the old Session Context tab used to duplicate.
//   Error rows are visually distinct (red dot + red left-stripe) but do
//   NOT auto-expand — the user clicks to see the full error body.

import { useMemo, useState } from 'react';

const STATUS_LABEL = {
    running: 'In progress',
    pending: 'Waiting',
    done:    'Success',
    error:   'Failed',
    stopped: 'Stopped',
    idle:    'Idle',
};

// Worst-status wins when rolling up sub-rows under a parent.
const STATUS_RANK = { error: 4, stopped: 3.5, pending: 3, running: 2, done: 1 };
function worstStatus(a, b) {
    if (!a) return b;
    if (!b) return a;
    return (STATUS_RANK[a] || 0) >= (STATUS_RANK[b] || 0) ? a : b;
}

function fmtClock(ts) {
    if (!ts) return '';
    try {
        const d = new Date(ts);
        return d.toLocaleTimeString('en-US', {
            hour12: false,
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
    } catch { return ''; }
}

function fmtDateHeader(ts) {
    if (!ts) return '';
    try {
        const d = new Date(ts);
        return d.toLocaleDateString('en-US', {
            weekday: 'short', day: '2-digit', month: 'short', year: 'numeric',
        });
    } catch { return ''; }
}

// Pull a short, human-readable snippet from an event's raw payload.
// Used in the expanded body, never on the compact row.
function extractSnippet(row) {
    const raw = row && row.raw;
    if (!raw) return '';
    const d = raw.data || {};
    if (raw.event === 'agent_complete' || raw.event === 'complete') {
        return typeof d.output === 'string' ? d.output : '';
    }
    if (raw.event === 'tool_call_start') {
        try {
            const args = d.arguments;
            const s = typeof args === 'string' ? args : JSON.stringify(args);
            return s ? `args: ${s}` : '';
        } catch { return ''; }
    }
    if (raw.event === 'tool_call_result') {
        try {
            const r = d.result;
            const s = typeof r === 'string' ? r : JSON.stringify(r);
            return s ? `result: ${s}` : '';
        } catch { return ''; }
    }
    if (raw.event === 'condition_routed') {
        return d.expression ? `expression: ${d.expression}` : '';
    }
    if (raw.event === 'kb_retrieval') {
        // Chunks render in their own dedicated block (KbChunks); the snippet
        // is just the query so the compact preview stays short.
        const q = raw.query || d.query;
        return q ? `query: ${q}` : '';
    }
    if (raw.event === 'error') {
        return typeof d.message === 'string' ? d.message : '';
    }
    return '';
}

// Render every retrieved RAG chunk with its source, per-chunk score (or an
// explicit "n/a" when the platform retriever doesn't expose one) and its
// FULL text. This is the "which chunks qualified and what was the score"
// surface the operator needs — nothing is truncated.
function KbChunks({ raw }) {
    if (!raw || raw.event !== 'kb_retrieval') return null;
    const chunks = Array.isArray(raw.chunks) ? raw.chunks : [];
    const conf = raw.confidence;
    return (
        <div className="debug-log-kb-block">
            <div className="debug-log-kb-summary">
                {raw.mode ? <span className="debug-log-kb-tag">mode: {raw.mode}</span> : null}
                <span className="debug-log-kb-tag">
                    {chunks.length} chunk{chunks.length === 1 ? '' : 's'} qualified
                </span>
                <span className="debug-log-kb-tag">
                    confidence: {conf === null || conf === undefined ? 'n/a' : Number(conf).toFixed(2)}
                </span>
            </div>
            {chunks.length === 0 ? (
                <div className="debug-log-row-sub">No chunks matched the query.</div>
            ) : (
                <ol className="debug-log-kb-chunks">
                    {chunks.map((c, i) => {
                        const score = c && c.score;
                        const hasScore = score !== null && score !== undefined;
                        const scoreLabel = hasScore
                            ? Number(score).toFixed(4)
                            : 'n/a (not exposed by retriever)';
                        return (
                            <li key={`kbc-${i}`} className="debug-log-kb-chunk">
                                <div className="debug-log-kb-chunk-head">
                                    <span className="debug-log-kb-chunk-idx">#{(c?.index ?? i) + 1}</span>
                                    {c?.source ? (
                                        <span className="debug-log-kb-chunk-src" title={c.source}>
                                            {c.source}
                                        </span>
                                    ) : <span className="debug-log-kb-chunk-src">(no source)</span>}
                                    <span className={`debug-log-kb-chunk-score${hasScore ? '' : ' na'}`}>
                                        score: {scoreLabel}
                                    </span>
                                    {c?.qualified ? (
                                        <span className="debug-log-kb-chunk-badge">qualified</span>
                                    ) : null}
                                </div>
                                <pre className="debug-log-kb-chunk-text">{c?.text || ''}</pre>
                            </li>
                        );
                    })}
                </ol>
            )}
        </div>
    );
}

function truncate(s, n = 320) {
    if (!s) return '';
    const flat = String(s).replace(/\s+/g, ' ').trim();
    return flat.length > n ? `${flat.slice(0, n)}…` : flat;
}

function StatusDot({ status }) {
    return <span className={`debug-log-dot${status ? ` ${status}` : ''}`} aria-hidden="true" />;
}

// Shared expand/collapse chevron. Rotation is driven by CSS off the parent's
// `.open` class, so this stays presentational.
function Chevron() {
    return (
        <span className="debug-log-chevron" aria-hidden="true">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" strokeWidth="2.5"
                 strokeLinecap="round" strokeLinejoin="round">
                <path d="M6 9l6 6 6-6" />
            </svg>
        </span>
    );
}

function StatusPill({ status }) {
    if (!status) return null;
    return (
        <span className={`debug-log-pill debug-log-pill-${status}`}>
            {STATUS_LABEL[status] || status}
        </span>
    );
}

function KindChip({ kind }) {
    if (!kind) return null;
    const slug = String(kind).toLowerCase().replace(/[^a-z0-9]+/g, '-');
    return <span className={`debug-log-kind debug-log-kind-${slug}`}>{kind}</span>;
}

function GeneratedFilesLine({ files }) {
    if (!Array.isArray(files) || files.length === 0) return null;
    const names = files
        .map((f) => (typeof f === 'string' ? f : f?.file_name || f?.name || ''))
        .filter(Boolean)
        .slice(0, 3)
        .join(', ');
    const more = files.length > 3 ? ` +${files.length - 3} more` : '';
    return (
        <div className="debug-log-row-sub">
            Generated {files.length} file{files.length === 1 ? '' : 's'}: {names}{more}
        </div>
    );
}

// Compute the small per-trace metadata bits the old Session Context tab
// showed on each Execution Trace row: agent name, char count, engine, and
// tokens. Rendered as `agent-default · 1,980 chars · engine · 42 tok`
// inside a node row's expanded body so the information lives beside the
// SSE payload it came from — one place, no duplication.
function traceMetaBits(step) {
    if (!step) return [];
    const bits = [];
    if (step.agent) bits.push(step.agent);
    if (typeof step.output === 'string') {
        const len = step.output.length;
        if (len) bits.push(`${len.toLocaleString()} chars`);
    } else if (step.output && typeof step.output === 'object') {
        const keys = Object.keys(step.output).slice(0, 4);
        if (keys.length) bits.push(`keys: ${keys.join(', ')}`);
    }
    if (step.engine) bits.push(step.engine);
    if (step.tokens && Number.isFinite(step.tokens)) {
        bits.push(`${step.tokens} tok`);
    }
    return bits;
}

// Shape a trace step so JsonModal (which expects {raw, nodeLabel}) can
// render it under the same viewer used for SSE payloads.
function traceStepToRow(step, idx) {
    return {
        id: `trace-${step.node_id || idx}-${idx}`,
        ts: null,
        nodeId: step.node_id || null,
        nodeLabel: step.agent || step.node_id || `Step ${idx + 1}`,
        title: 'Execution trace step',
        detail: '',
        status: step.status || 'done',
        raw: step,
    };
}

// Body rendered inside an expanded row. Same shape whether the row is a
// parent rollup or a single-event leaf — keeps the layout consistent.
// When a matching execution-trace step is passed in, its metadata is
// rendered alongside the SSE payload and a second "View trace JSON" link
// is exposed. This is the merge point that eliminates the old Session
// Context tab: everything it surfaced now lives inside the node's own
// expanded body.
function ExpandedBody({ row, onViewJSON, traceStep, traceStepIndex }) {
    const snippet = truncate(extractSnippet(row));
    const traceBits = traceMetaBits(traceStep);
    return (
        <div className="debug-log-row-body">
            <div className="debug-log-row-meta">
                <KindChip kind={row.kind} />
                <StatusPill status={row.status} />
                {row.tsClosed && row.tsClosed !== row.ts ? (
                    <span className="debug-log-row-duration">
                        {`closed at ${fmtClock(row.tsClosed)}`}
                    </span>
                ) : null}
                {traceBits.length > 0 ? (
                    <span className="debug-log-row-duration">
                        {traceBits.join(' · ')}
                    </span>
                ) : null}
            </div>
            {row.detail ? <div className="debug-log-row-sub">{row.detail}</div> : null}
            {row.kbHint ? (
                <div className="debug-log-row-sub debug-log-row-kb">{row.kbHint}</div>
            ) : null}
            <GeneratedFilesLine files={row.generatedFiles} />
            {/* RAG chunks (source + score + full text) render in full here. */}
            <KbChunks raw={row.raw} />
            {snippet ? <pre className="debug-log-row-snippet">{snippet}</pre> : null}
            {row.raw ? (
                <button
                    type="button"
                    className="debug-log-json-link"
                    onClick={(e) => { e.stopPropagation(); onViewJSON(row); }}
                >
                    View JSON
                </button>
            ) : null}
            {traceStep ? (
                <button
                    type="button"
                    className="debug-log-json-link"
                    onClick={(e) => {
                        e.stopPropagation();
                        onViewJSON(traceStepToRow(traceStep, traceStepIndex ?? 0));
                    }}
                >
                    View trace JSON
                </button>
            ) : null}
        </div>
    );
}

// One leaf row — used both at top level for ungrouped events and as a
// nested child inside an expanded parent. Always starts collapsed.
function LeafRow({ row, onViewJSON, nested = false, traceStep, traceStepIndex }) {
    const [open, setOpen] = useState(false);
    const status = row.status || 'done';
    const handleToggle = () => setOpen((v) => !v);
    return (
        <li className={`debug-log-row debug-log-row-${status}${nested ? ' nested' : ''}${open ? ' open' : ''}`}>
            <button
                type="button"
                className="debug-log-row-head"
                onClick={handleToggle}
                aria-expanded={open}
            >
                <StatusDot status={status} />
                <div className="debug-log-row-text">
                    <div className="debug-log-row-title">{row.nodeLabel || row.title}</div>
                    {row.title && row.nodeLabel && row.title !== row.nodeLabel ? (
                        <div className="debug-log-row-sub">{row.title}</div>
                    ) : null}
                </div>
                <span className="debug-log-time">{fmtClock(row.tsClosed || row.ts)}</span>
                <Chevron />
            </button>
            {open ? (
                <ExpandedBody
                    row={row}
                    onViewJSON={onViewJSON}
                    traceStep={traceStep}
                    traceStepIndex={traceStepIndex}
                />
            ) : null}
        </li>
    );
}

// A parent rollup — used when multiple consecutive events share the same
// nodeId. Shows the rolled-up label/status on the closed row; expanding
// reveals each sub-event as a nested LeafRow.
function ParentRow({ rollup, onViewJSON, traceStep, traceStepIndex }) {
    const [open, setOpen] = useState(false);
    const status = rollup.status;
    return (
        <li className={`debug-log-row debug-log-row-${status} parent${open ? ' open' : ''}`}>
            <button
                type="button"
                className="debug-log-row-head"
                onClick={() => setOpen((v) => !v)}
                aria-expanded={open}
            >
                <StatusDot status={status} />
                <div className="debug-log-row-text">
                    <div className="debug-log-row-title">
                        {rollup.nodeLabel}
                        <span className="debug-log-row-count">
                            {` (${rollup.children.length} step${rollup.children.length === 1 ? '' : 's'})`}
                        </span>
                    </div>
                    <div className="debug-log-row-sub">{rollup.summary}</div>
                </div>
                <span className="debug-log-time">{fmtClock(rollup.tsLast)}</span>
                <Chevron />
            </button>
            {open ? (
                <>
                    {traceStep ? (
                        <div className="debug-log-row-body">
                            <div className="debug-log-row-meta">
                                <span className="debug-log-row-duration">
                                    {traceMetaBits(traceStep).join(' · ') || 'execution trace'}
                                </span>
                                <button
                                    type="button"
                                    className="debug-log-json-link"
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        onViewJSON(traceStepToRow(traceStep, traceStepIndex ?? 0));
                                    }}
                                >
                                    View trace JSON
                                </button>
                            </div>
                        </div>
                    ) : null}
                    <ol className="debug-log-children">
                        {rollup.children.map((c) => (
                            <LeafRow key={c.id} row={c} onViewJSON={onViewJSON} nested />
                        ))}
                    </ol>
                </>
            ) : null}
        </li>
    );
}

// Reduce the flat rows[] into either LeafRow items or ParentRow rollups.
// Grouping is consecutive-only and keyed by nodeId — preserves event
// order in the UI and means "Run started" / "Run completed" / errors
// without a nodeId stay as their own leaf rows.
function buildItems(rows) {
    const items = [];
    let bucket = null;

    const flushBucket = () => {
        if (!bucket) return;
        if (bucket.children.length === 1) {
            // A single event for a node — render it as a leaf, not a
            // rollup. Avoids "(1 step)" labels on common short paths.
            items.push({ kind: 'leaf', row: bucket.children[0] });
        } else {
            const last = bucket.children[bucket.children.length - 1];
            items.push({
                kind: 'parent',
                rollup: {
                    nodeId: bucket.nodeId,
                    nodeLabel: bucket.children[0].nodeLabel,
                    children: bucket.children,
                    status: bucket.children.reduce((s, c) => worstStatus(s, c.status), null),
                    summary: last.title || `${bucket.children.length} events`,
                    tsLast: last.tsClosed || last.ts,
                },
            });
        }
        bucket = null;
    };

    for (const r of rows) {
        // Rows without a nodeId (run lifecycle, swarm planner, etc.) are
        // never grouped — they live as standalone leaves.
        if (!r.nodeId) {
            flushBucket();
            items.push({ kind: 'leaf', row: r });
            continue;
        }
        if (!bucket || bucket.nodeId !== r.nodeId) {
            flushBucket();
            bucket = { nodeId: r.nodeId, children: [r] };
        } else {
            bucket.children.push(r);
        }
    }
    flushBucket();
    return items;
}

function JsonModal({ row, onClose }) {
    // Copy-feedback flag flips to true for ~1.5s after a successful
    // navigator.clipboard write so the button label confirms the action
    // without introducing a toast dependency. Hooks must be declared
    // unconditionally so keep this above the null-guard below.
    const [copied, setCopied] = useState(false);
    if (!row) return null;
    const payload = row.raw == null ? {
        id: row.id, ts: row.ts, nodeId: row.nodeId, nodeLabel: row.nodeLabel,
        title: row.title, detail: row.detail, status: row.status,
    } : row.raw;
    const pretty = JSON.stringify(payload, null, 2);
    const handleCopy = async () => {
        // navigator.clipboard is unavailable in insecure contexts (some
        // internal test rigs run over http://). Fall back to a hidden
        // textarea + document.execCommand so the button still works.
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(pretty);
            } else {
                const ta = document.createElement('textarea');
                ta.value = pretty;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch (_err) {
            // Best-effort — leave label unchanged on failure.
        }
    };
    return (
        <div className="debug-log-modal-backdrop" onClick={onClose}>
            <div
                className="debug-log-modal"
                role="dialog"
                aria-label="Event payload"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="debug-log-modal-header">
                    <span className="debug-log-modal-title">
                        {row.nodeLabel || 'Event'} — payload
                    </span>
                    <div className="debug-log-modal-actions">
                        <button
                            type="button"
                            className="debug-log-modal-copy"
                            onClick={handleCopy}
                            aria-label="Copy JSON payload to clipboard"
                        >
                            {copied ? 'Copied!' : 'Copy JSON'}
                        </button>
                        <button
                            type="button"
                            className="debug-log-modal-close"
                            onClick={onClose}
                            aria-label="Close JSON viewer"
                        >×</button>
                    </div>
                </div>
                <pre className="debug-log-modal-pre">
                    {pretty}
                </pre>
            </div>
        </div>
    );
}

// Build an O(1) lookup so a given EXECUTION row can find its matching
// execution-trace step by nodeId. The trace only arrives on `complete`,
// so during a run this map is empty and every node row falls back to
// showing SSE-only content — exactly what the old Debug Logs tab did.
function traceStepByNodeId(executionTrace) {
    const map = new Map();
    if (!Array.isArray(executionTrace)) return map;
    executionTrace.forEach((step, idx) => {
        const nid = step && step.node_id;
        if (nid && !map.has(nid)) map.set(nid, { step, idx });
    });
    return map;
}

// Turn a trace step's `output` field into a preview + full string for
// the inline per-node "Output" leaf row. Falls back to a JSON preview
// when the step's output is a structured object.
function traceOutputStrings(step) {
    if (!step) return null;
    const out = step.output;
    let full = '';
    if (typeof out === 'string') full = out;
    else if (out && typeof out === 'object') {
        try { full = JSON.stringify(out, null, 2); } catch { full = String(out); }
    } else if (out != null) full = String(out);
    if (!full) return null;
    const flat = full.replace(/\s+/g, ' ').trim();
    const preview = flat.length > 80 ? `${flat.slice(0, 80)}…` : flat;
    return { full, preview };
}

// Build a synthetic row shape (fed straight into LeafRow) that renders
// the per-node output produced by an execution_trace step. It sits
// directly below the node row in the chronological flow.
function nodeOutputRow(nodeLabel, nodeId, step, stepIndex) {
    const strs = traceOutputStrings(step);
    if (!strs) return null;
    return {
        id: `trace-out-${nodeId || stepIndex}-${stepIndex}`,
        ts: null,
        nodeId,
        nodeLabel: `${nodeLabel || 'Node'} ▸ Output`,
        title: strs.preview,
        detail: strs.full,
        status: step.status || 'done',
        kind: 'Output',
        raw: step,
    };
}

// The single unified timeline, rendered as a single top-to-bottom
// chronological flow:
//
//     Input        (user prompt)
//     Start        (workflow_start)
//     Node A       (agent_start … agent_complete rolled up)
//     Node A ▸ Output   (from execution_trace[A].output)
//     Node B
//     Node B ▸ Output
//     End
//     Tokens (approx)
//     Status
//
// Every fact appears exactly once — the per-node Output rows are the
// only place a node's produced text is shown, so the old "Final Output"
// row is dropped when it would just be a copy of the last node's output.
function UnifiedTimeline({ run, onViewJSON }) {
    const {
        rows, startedAt, runId, status,
        currentInput, finalOutput, executionTrace,
    } = run;

    const traceMap = useMemo(
        () => traceStepByNodeId(executionTrace),
        [executionTrace],
    );

    // Split kind='Input'/'Output'/'Tokens' out of the middle stream so
    // we can position them at the correct chronological anchors: Input
    // at the very top, Tokens near the end, and Output either inlined
    // per-node or as a single final row if it differs from the last
    // node's trace output.
    const partitioned = useMemo(() => {
        const inputR = [];
        const outputR = [];
        const tokenR = [];
        const execRows = [];
        for (const r of rows) {
            const k = r.kind || '';
            if (k === 'Input') inputR.push(r);
            else if (k === 'Output') outputR.push(r);
            else if (k === 'Tokens (approx)') tokenR.push(r);
            else execRows.push(r);
        }
        return { inputR, outputR, tokenR, execRows };
    }, [rows]);

    // Roll up the middle stream (Start / node events / End) into leaf +
    // parent items, ignoring the row.group split — the chronological
    // flow doesn't need "Flow Initialization" vs. "Run" section headers
    // any more since Start is right at the top.
    const executionItems = useMemo(
        () => buildItems(partitioned.execRows),
        [partitioned.execRows],
    );

    // Nothing to show for THIS run — the parent renders the global empty
    // state when no run has any rows.
    if (!runId || rows.length === 0) return null;

    // ---- Compose the flow ------------------------------------------------
    // The result is a flat list of <LeafRow>/<ParentRow> nodes rendered
    // inside a single <ol.debug-log-rows>. No section headers — just one
    // continuous stream so the reader follows the workflow's actual
    // execution top-to-bottom.

    const flow = [];

    // 1. Input row (row-based, fallback to structured currentInput).
    if (partitioned.inputR.length > 0) {
        for (const r of partitioned.inputR) {
            flow.push(<LeafRow key={r.id} row={r} onViewJSON={onViewJSON} />);
        }
    } else if (currentInput) {
        const synth = {
            id: 'synth-input',
            ts: startedAt,
            nodeId: null,
            nodeLabel: 'Input',
            title: currentInput.length > 80 ? `${currentInput.slice(0, 80)}…` : currentInput,
            detail: currentInput,
            status: 'done',
            kind: 'Input',
        };
        flow.push(<LeafRow key={synth.id} row={synth} onViewJSON={onViewJSON} />);
    }

    // 2. Interleave execution rows with their per-node Output pseudo-row.
    //    Track which trace steps we've already surfaced so we can decide
    //    at the end whether the final Output row is redundant.
    const surfacedTraceIndices = new Set();
    let lastSurfacedTraceOutput = '';

    for (let i = 0; i < executionItems.length; i++) {
        const item = executionItems[i];
        if (item.kind === 'parent') {
            const nid = item.rollup.nodeId;
            const trace = nid ? traceMap.get(nid) : null;
            flow.push(
                <ParentRow
                    key={`p-${i}`}
                    rollup={item.rollup}
                    onViewJSON={onViewJSON}
                    traceStep={trace ? trace.step : null}
                    traceStepIndex={trace ? trace.idx : null}
                />
            );
            if (trace) {
                const outRow = nodeOutputRow(item.rollup.nodeLabel, nid, trace.step, trace.idx);
                if (outRow) {
                    flow.push(<LeafRow key={outRow.id} row={outRow} onViewJSON={onViewJSON} />);
                    surfacedTraceIndices.add(trace.idx);
                    lastSurfacedTraceOutput = outRow.detail || '';
                }
            }
        } else {
            const nid = item.row.nodeId;
            const trace = nid ? traceMap.get(nid) : null;
            flow.push(
                <LeafRow
                    key={item.row.id}
                    row={item.row}
                    onViewJSON={onViewJSON}
                    traceStep={trace ? trace.step : null}
                    traceStepIndex={trace ? trace.idx : null}
                />
            );
            if (trace) {
                const outRow = nodeOutputRow(item.row.nodeLabel, nid, trace.step, trace.idx);
                if (outRow) {
                    flow.push(<LeafRow key={outRow.id} row={outRow} onViewJSON={onViewJSON} />);
                    surfacedTraceIndices.add(trace.idx);
                    lastSurfacedTraceOutput = outRow.detail || '';
                }
            }
        }
    }

    // 2b. Any trace steps whose node_id didn't map to an execution row
    //     (rare — happens on subagent-only workflows). Surface them so
    //     no per-node output is lost.
    if (Array.isArray(executionTrace)) {
        executionTrace.forEach((step, idx) => {
            if (surfacedTraceIndices.has(idx)) return;
            const label = step.agent || step.node_id || `Step ${idx + 1}`;
            const outRow = nodeOutputRow(label, step.node_id, step, idx);
            if (outRow) {
                flow.push(<LeafRow key={outRow.id} row={outRow} onViewJSON={onViewJSON} />);
                lastSurfacedTraceOutput = outRow.detail || '';
            }
        });
    }

    // 3. Final Output row — ONLY if it isn't already the same text we
    //    surfaced inline as the last node's output. Prevents the double
    //    listing the previous layout suffered from.
    const normalize = (s) => (typeof s === 'string' ? s.trim() : '');
    if (partitioned.outputR.length > 0) {
        for (const r of partitioned.outputR) {
            const rowText = normalize(r.detail || r.title);
            if (rowText && rowText === normalize(lastSurfacedTraceOutput)) continue;
            flow.push(<LeafRow key={r.id} row={r} onViewJSON={onViewJSON} />);
        }
    } else if (finalOutput && normalize(finalOutput) !== normalize(lastSurfacedTraceOutput)) {
        const synth = {
            id: 'synth-output',
            ts: null,
            nodeId: null,
            nodeLabel: 'Output',
            title: finalOutput.length > 80 ? `${finalOutput.slice(0, 80)}…` : finalOutput,
            detail: finalOutput,
            status: 'done',
            kind: 'Output',
        };
        flow.push(<LeafRow key={synth.id} row={synth} onViewJSON={onViewJSON} />);
    }

    // 4. Tokens estimate row(s) — the total across the whole workflow.
    for (const r of partitioned.tokenR) {
        flow.push(<LeafRow key={r.id} row={r} onViewJSON={onViewJSON} />);
    }

    // 5. Final Status pill row.
    if (status) {
        flow.push(
            <li key="status-row" className={`debug-log-row debug-log-row-${status}`}>
                <div className="debug-log-row-head" style={{ cursor: 'default' }}>
                    <StatusDot status={status} />
                    <div className="debug-log-row-text">
                        <div className="debug-log-row-title">Status</div>
                        <div className="debug-log-row-sub">
                            <StatusPill status={status} />
                        </div>
                    </div>
                    <span aria-hidden="true" />
                    <span aria-hidden="true" />
                </div>
            </li>
        );
    }

    // Just the per-run row flow — the enclosing RunSection provides the
    // collapsible "Run N" header, date and status pill.
    return (
        <ol className="debug-log-rows">
            {flow}
        </ol>
    );
}

// One collapsible run block: "Run N — HH:MM:SS" header + status pill, with
// the run's full UnifiedTimeline inside. Newest run is auto-expanded; older
// archived runs start collapsed so the user can scroll back and click to
// inspect any prior run in the same chat.
function RunSection({ run, label, defaultOpen, onViewJSON }) {
    const [open, setOpen] = useState(!!defaultOpen);
    const rowCount = Array.isArray(run.rows) ? run.rows.length : 0;
    return (
        <li className={`debug-log-group${open ? ' open' : ''}`}>
            <button
                type="button"
                className="debug-log-group-header debug-log-group-toggle"
                onClick={() => setOpen((v) => !v)}
                aria-expanded={open}
            >
                <Chevron />
                <span className="debug-log-group-title">
                    {label}
                    <span className="debug-log-row-count">{` · ${rowCount} row${rowCount === 1 ? '' : 's'}`}</span>
                </span>
                <span className="debug-log-group-date">{fmtDateHeader(run.startedAt)} {fmtClock(run.startedAt)}</span>
                <StatusPill status={run.status} />
            </button>
            {open ? (
                <UnifiedTimeline run={run} onViewJSON={onViewJSON} />
            ) : null}
        </li>
    );
}

export default function DebugLogView({ runContext, onClose, onMinimize }) {
    // Single unified timeline — no tabs. The old Debug Logs / Session
    // Context split duplicated ~44% of its rows (input, output, status,
    // node events). UnifiedTimeline lays out INPUT → EXECUTION → OUTPUT
    // → METADATA end-to-end so each fact appears exactly once.
    const [jsonRow, setJsonRow] = useState(null);
    const handleMinimize = onMinimize || onClose;

    // Compose every run for this chat: the CURRENT run (top-level runContext
    // fields) plus the archived prior runs in `runHistory` (already stored
    // newest-first). Only runs that produced rows are shown. Runs are
    // numbered oldest→newest ("Run 1" is the first one triggered) but
    // rendered newest-first, with the newest auto-expanded.
    const runsNewestFirst = useMemo(() => {
        const history = Array.isArray(runContext?.runHistory) ? runContext.runHistory : [];
        const currentHasRows = runContext?.runId && (runContext.rows || []).length > 0;
        const total = history.length + (currentHasRows ? 1 : 0);
        const list = [];
        if (currentHasRows) list.push({ run: runContext, number: total });
        // history[0] is the most-recently archived; number down from there.
        const archivedTop = total - (currentHasRows ? 1 : 0);
        history.forEach((h, i) => list.push({ run: h, number: archivedTop - i }));
        return list;
    }, [runContext]);

    return (
        <div className="debug-log-view" data-testid="debug-log-view">
            <div className="debug-log-header">
                <div className="debug-log-header-title">
                    <span className="debug-log-header-icon" aria-hidden="true">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                             stroke="currentColor" strokeWidth="2"
                             strokeLinecap="round" strokeLinejoin="round">
                            <rect x="8" y="6" width="8" height="14" rx="4" />
                            <path d="M12 6V3M5 9l3 1M19 9l-3 1M5 15l3-1M19 15l-3-1M9 20l-2 2M15 20l2 2" />
                        </svg>
                    </span>
                    <span>Debug Log</span>
                </div>
                <div className="debug-log-header-actions">
                    <button
                        type="button"
                        className="debug-log-header-btn"
                        onClick={handleMinimize}
                        title="Minimize — keeps the log; reopen from the bug icon"
                        aria-label="Minimize Debug Log"
                    >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                             stroke="currentColor" strokeWidth="2"
                             strokeLinecap="round" strokeLinejoin="round">
                            <path d="M5 19h14" />
                        </svg>
                    </button>
                    <button
                        type="button"
                        className="debug-log-header-btn debug-log-header-close"
                        onClick={onClose}
                        title="Close Debug Log"
                        aria-label="Close Debug Log"
                    >×</button>
                </div>
            </div>
            <div className="debug-log-body">
                {runsNewestFirst.length === 0 ? (
                    <div className="debug-log-empty">
                        <div className="debug-log-empty-title">No debug events yet</div>
                        <div className="debug-log-empty-detail">
                            Run a workflow to see the input, per-node execution + output,
                            and total tokens appear here in real time. Logs persist across
                            runs in this chat — start a new chat to clear them.
                        </div>
                    </div>
                ) : (
                    <ol className="debug-log-list">
                        {runsNewestFirst.map((entry, idx) => (
                            <RunSection
                                key={entry.run.runId || `run-${entry.number}`}
                                run={entry.run}
                                label={`Run ${entry.number}`}
                                defaultOpen={idx === 0}
                                onViewJSON={setJsonRow}
                            />
                        ))}
                    </ol>
                )}
            </div>
            <JsonModal row={jsonRow} onClose={() => setJsonRow(null)} />
        </div>
    );
}
