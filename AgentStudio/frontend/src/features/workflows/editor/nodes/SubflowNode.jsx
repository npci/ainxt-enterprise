// SPDX-License-Identifier: MIT
import { Handle, Position } from '@xyflow/react';
import { motion } from 'framer-motion';
import useWorkflowStore from '../../../../store/workflowStore';

// Stacked-squares icon — visually distinguishes the "linked existing asset"
// node from a fresh inline Agent. Conveys "embedded sub-flow".
const SubflowIcon = () => (
    <svg
        width="24"
        height="24"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        xmlns="http://www.w3.org/2000/svg"
    >
        <rect x="3" y="3" width="13" height="13" rx="2" />
        <rect x="8" y="8" width="13" height="13" rx="2" />
    </svg>
);

function SubflowNode({ id, data = {}, selected }) {
    const activeNodeIds = useWorkflowStore((state) => state.activeNodeIds);
    const isExecuting = activeNodeIds.includes(id);
    const status = data.status || data.executionStatus || data.state || '';

    const cls = [
        'node-block',
        'node-block--subflow',
        selected ? 'selected' : '',
        isExecuting ? 'executing' : '',
        status === 'success' ? 'success' : '',
        status === 'error' || status === 'failed' ? 'error' : '',
    ].filter(Boolean).join(' ');

    const refName = data.refName || '';
    const kindLabel = data.kind === 'workflow' ? 'Workflow' : 'Agent';
    const displayName = refName || 'Pick an asset…';

    return (
        <motion.div
            className={cls}
            data-node-type="subflow"
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            whileHover={{ scale: 1.035, y: -3 }}
            transition={{ type: 'spring', stiffness: 420, damping: 30, mass: 0.7 }}
        >
            <Handle type="target" position={Position.Top} id="target" />

            <div className="node-block-body">
                <div className="node-block-icon">
                    <SubflowIcon />
                </div>
                <div className="node-block-text">
                    <h4 className="node-block-name">{displayName}</h4>
                    <span className="node-block-label">
                        {refName ? `Existing ${kindLabel}` : 'Existing Workflow / Agent'}
                    </span>
                </div>
            </div>

            <Handle type="source" position={Position.Bottom} id="source" />
        </motion.div>
    );
}

export default SubflowNode;
