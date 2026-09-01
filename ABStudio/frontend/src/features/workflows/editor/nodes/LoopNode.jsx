// SPDX-License-Identifier: Apache-2.0
import { Handle, Position } from '@xyflow/react';
import { motion } from 'framer-motion';
import useWorkflowStore from '../../../../store/workflowStore';

const LoopIcon = () => (
    <svg
        width="24"
        height="24"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        xmlns="http://www.w3.org/2000/svg"
    >
        <path d="M17 2l4 4-4 4" />
        <path d="M3 11v-1a4 4 0 0 1 4-4h14" />
        <path d="M7 22l-4-4 4-4" />
        <path d="M21 13v1a4 4 0 0 1-4 4H3" />
    </svg>
);

// Show the loop's mode in plain language on the canvas so the user never
// reads the raw dotted path. The dotted path is still kept in node.data
// for the engine; this is purely the rendered label.
function humanisePath(path) {
    if (!path || path === 'input') return 'each item';
    const last = String(path).split('.').filter(Boolean).pop();
    if (!last || last === 'items') return 'each item';
    const spaced = last
        .replace(/[_-]+/g, ' ')
        .replace(/([a-z])([A-Z])/g, '$1 $2')
        .toLowerCase();
    return `each ${spaced}`;
}

function summarize(data = {}) {
    const mode = data.mode || 'for_each';
    if (mode === 'for_each') return `For ${humanisePath(data.itemsExpression)}`;
    if (mode === 'while') {
        // Loop config now persists a single case; count the rule rows inside
        // it so the label reflects what the user actually configured (e.g.
        // "While 1 Condition Match" / "While 3 Conditions Match").
        const firstCase = (data.cases || [])[0];
        const n = (firstCase?.conditions || []).length;
        return `While ${n} Condition${n === 1 ? '' : 's'} Match`;
    }
    if (mode === 'count') return `Repeat ${data.count ?? 3} times`;
    return mode;
}

function LoopNode({ id, data = {}, selected }) {
    const activeNodeIds = useWorkflowStore((state) => state.activeNodeIds);
    const progress = useWorkflowStore((state) => state.loopProgress[id]);
    const isExecuting = activeNodeIds.includes(id);
    const status = data.status || data.executionStatus || data.state || '';

    const cls = [
        'node-block',
        'node-block--loop',
        selected ? 'selected' : '',
        isExecuting ? 'executing' : '',
        status === 'success' ? 'success' : '',
        status === 'error' || status === 'failed' ? 'error' : '',
    ].filter(Boolean).join(' ');

    // Round 2 of 5 — current/total badge that fills in as iterations land.
    // For `while` mode total is unknown; show "Round N" with no denominator.
    const running = !!(progress && progress.running);
    const currentRound = progress ? (progress.index ?? 0) + 1 : 0;
    const total = progress ? progress.total : null;
    const pct = total && total > 0 ? Math.min(100, Math.round((currentRound / total) * 100)) : null;

    return (
        <motion.div
            className={cls}
            data-node-type="loop"
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            whileHover={{ scale: 1.035, y: -3 }}
            transition={{ type: 'spring', stiffness: 420, damping: 30, mass: 0.7 }}
        >
            {/* Top: incoming flow. Also the back-edge target for the body subgraph. */}
            <Handle type="target" position={Position.Top} id="target" />

            <div className="node-block-body">
                <div className="node-block-icon">
                    <LoopIcon />
                </div>
                <div className="node-block-text">
                    <h4 className="node-block-name">Loop</h4>
                    <span className="node-block-label">{summarize(data)}</span>
                </div>
                {running && (
                    <span className="loop-progress-badge" title="Iterations completed / planned">
                        {total != null ? `${currentRound} / ${total}` : `Round ${currentRound}`}
                    </span>
                )}
            </div>

            {running && pct != null && (
                <div className="loop-progress-bar" aria-hidden="true">
                    <div className="loop-progress-bar-fill" style={{ width: `${pct}%` }} />
                </div>
            )}

            {/* Right: 'body' handle — iterates the downstream subgraph until termination. */}
            <Handle
                type="source"
                position={Position.Right}
                id="body"
                className="loop-handle-body"
                title="Body (iterates)"
            />

            {/* Bottom: 'exit' handle — continues here after the loop terminates. */}
            <Handle
                type="source"
                position={Position.Bottom}
                id="exit"
                className="loop-handle-exit"
                title="Exit (after loop)"
            />
        </motion.div>
    );
}

export default LoopNode;
