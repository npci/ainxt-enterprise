// SPDX-License-Identifier: Apache-2.0
import { Handle, Position } from '@xyflow/react';
import { motion } from 'framer-motion';
import useWorkflowStore from '../../../../store/workflowStore';
import { buildCombinedExpressionPreview } from '../../../../constants/operators';

// Branching fork: 1 input → 2 paths (decision tree glyph)
const ConditionIcon = () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"
        stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
        {/* Stem */}
        <path d="M12 3v5" />
        {/* Fork arms */}
        <path d="M12 8 L6 14" />
        <path d="M12 8 L18 14" />
        {/* Downward legs */}
        <path d="M6 14v4" />
        <path d="M18 14v4" />
        {/* End indicators */}
        <circle cx="6" cy="20" r="1.25" fill="currentColor" stroke="none" />
        <circle cx="18" cy="20" r="1.25" fill="currentColor" stroke="none" />
    </svg>
);

function ConditionNode({ id, data, selected }) {
    const activeNodeIds = useWorkflowStore((state) => state.activeNodeIds);
    const isExecuting = activeNodeIds.includes(id);
    const status = data.status || data.executionStatus || data.state || '';

    const cases = data?.cases || [];

    const cls = [
        'node-block',
        'node-block--condition',
        'condition-node-new',
        selected ? 'selected' : '',
        isExecuting ? 'executing' : '',
        status === 'success' ? 'success' : '',
        status === 'error' || status === 'failed' ? 'error' : '',
    ].filter(Boolean).join(' ');

    return (
        <motion.div
            className={cls}
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            whileHover={{ scale: 1.025, y: -3 }}
            transition={{ type: 'spring', stiffness: 420, damping: 30, mass: 0.7 }}
        >
            {/* Top input handle */}
            <Handle
                type="target"
                position={Position.Top}
                id="target"
                className="condition-new-handle-target"
            />

            {/* Header */}
            <div className="condition-new-header">
                <div className="condition-new-icon">
                    <ConditionIcon />
                </div>
                <span className="condition-new-title">If / else</span>
            </div>

            {/* Case rows */}
            <div className="condition-new-cases">
                {cases.map((caseItem) => {
                    // Legacy nodes may carry a raw `expression` string; new
                    // nodes use the structured `conditions[]` shape.
                    const expression =
                        buildCombinedExpressionPreview(
                            caseItem.conditions || [],
                            caseItem.logic || 'AND'
                        ) || caseItem.expression || '';
                    const isUnconfigured = !expression;
                    const label = caseItem.label || caseItem.name || 'Case';

                    return (
                        <div key={caseItem.id} className="condition-new-case-row">
                            <div
                                className={`condition-new-expression${isUnconfigured ? ' condition-new-expression--empty' : ''}`}
                                title={expression || 'Configure this case in the side panel'}
                            >
                                {isUnconfigured ? `${label} — not configured` : expression}
                            </div>
                            <Handle
                                type="source"
                                position={Position.Right}
                                id={caseItem.id}
                                className="condition-new-handle-source"
                            />
                        </div>
                    );
                })}

                {/* Else row */}
                <div className="condition-new-else-row">
                    <div className="condition-new-else-label">Else</div>
                    <Handle
                        type="source"
                        position={Position.Right}
                        id="else"
                        className="condition-new-handle-source condition-new-handle-else"
                    />
                </div>
            </div>
        </motion.div>
    );
}

export default ConditionNode;
