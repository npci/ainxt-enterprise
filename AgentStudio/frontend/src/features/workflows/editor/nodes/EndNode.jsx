// SPDX-License-Identifier: MIT
import { Handle, Position } from '@xyflow/react';
import { motion } from 'framer-motion';
import useWorkflowStore from '../../../../store/workflowStore';

// Solid square — standard stop / completion signal
const EndIcon = () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg">
        <rect x="4" y="4" width="16" height="16" rx="3" />
    </svg>
);

function EndNode({ id, data = {}, selected }) {
    const activeNodeIds = useWorkflowStore((state) => state.activeNodeIds);
    const isExecuting = activeNodeIds.includes(id);
    const status = data.status || data.executionStatus || data.state || '';

    const cls = [
        'node-block',
        'node-block--end',
        selected ? 'selected' : '',
        isExecuting ? 'executing' : '',
        status === 'success' ? 'success' : '',
        status === 'error' || status === 'failed' ? 'error' : '',
    ].filter(Boolean).join(' ');

    return (
        <motion.div
            className={cls}
            data-node-type="end"
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            whileHover={{ scale: 1.035, y: -3 }}
            transition={{ type: 'spring', stiffness: 420, damping: 30, mass: 0.7 }}
        >
            <div className="node-block-body">
                <div className="node-block-icon">
                    <EndIcon />
                </div>
                <div className="node-block-text">
                    <span className="node-block-name">End</span>
                    <span className="node-block-label">Complete</span>
                </div>
            </div>

            <Handle
                type="target"
                position={Position.Top}
                id="target"
            />
        </motion.div>
    );
}

export default EndNode;
