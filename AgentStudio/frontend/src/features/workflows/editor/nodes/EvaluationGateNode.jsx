// SPDX-License-Identifier: MIT
import { Handle, Position } from '@xyflow/react';
import { motion } from 'framer-motion';
import useWorkflowStore from '../../../../store/workflowStore';

// Evaluation gate: runs an LLM judge against the upstream agent's output
// and routes through the 'pass' handle when score >= threshold, otherwise
// through 'fail'. Backend dispatch lives in NativeEngine._traverse →
// _route_evaluation_gate (app/engine/native_engine.py). This component is
// the minimal P2 stub; styling parity with ConditionNode lands in a
// follow-up alongside the side-panel editor.
const GateIcon = () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"
        stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
        {/* Scales (judge motif) */}
        <path d="M12 3v18" />
        <path d="M7 8l5-3 5 3" />
        <path d="M5 12h4l-2 4-2-4z" />
        <path d="M15 12h4l-2 4-2-4z" />
    </svg>
);

function EvaluationGateNode({ id, data, selected }) {
    const activeNodeIds = useWorkflowStore((state) => state.activeNodeIds);
    const isExecuting = activeNodeIds.includes(id);
    const status = data?.status || data?.executionStatus || data?.state || '';

    const criteria = (data?.criteria || '').trim();
    const threshold = typeof data?.threshold === 'number' ? data.threshold : 0.7;
    const isUnconfigured = !criteria;

    const cls = [
        'node-block',
        'node-block--evaluation-gate',
        'evaluation-gate-node',
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
                className="evaluation-gate-handle-target"
            />

            {/* Header */}
            <div className="evaluation-gate-header">
                <div className="evaluation-gate-icon">
                    <GateIcon />
                </div>
                <span className="evaluation-gate-title">Evaluation gate</span>
            </div>

            {/* Body */}
            <div className="evaluation-gate-body">
                <div
                    className={`evaluation-gate-criteria${isUnconfigured ? ' evaluation-gate-criteria--empty' : ''}`}
                    title={criteria || 'Set the judge criteria in the side panel'}
                >
                    {isUnconfigured ? 'No criteria — configure in side panel' : criteria}
                </div>
                <div className="evaluation-gate-threshold">
                    Threshold: {threshold.toFixed(2)}
                </div>
            </div>

            {/* Pass / Fail handles — names mirror the backend gate_edges map */}
            <div className="evaluation-gate-rows">
                <div className="evaluation-gate-row evaluation-gate-row--pass">
                    <span className="evaluation-gate-row-label">Pass</span>
                    <Handle
                        type="source"
                        position={Position.Right}
                        id="pass"
                        className="evaluation-gate-handle-pass"
                    />
                </div>
                <div className="evaluation-gate-row evaluation-gate-row--fail">
                    <span className="evaluation-gate-row-label">Fail</span>
                    <Handle
                        type="source"
                        position={Position.Right}
                        id="fail"
                        className="evaluation-gate-handle-fail"
                    />
                </div>
            </div>
        </motion.div>
    );
}

export default EvaluationGateNode;
