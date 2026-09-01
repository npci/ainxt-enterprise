// SPDX-License-Identifier: Apache-2.0
import { Handle, Position } from '@xyflow/react';
import { motion } from 'framer-motion';
import useWorkflowStore from '../../../../store/workflowStore';

// Simple play triangle — standard start/trigger signal
const StartIcon = () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg">
        <path d="M6 4v16l14-8z" />
    </svg>
);

function StartNode({ id, data = {}, selected }) {
    const activeNodeIds = useWorkflowStore((state) => state.activeNodeIds);
    const isExecuting = activeNodeIds.includes(id);
    const status = data.status || data.executionStatus || data.state || '';

    const cls = [
        'node-block',
        'node-block--start',
        selected ? 'selected' : '',
        isExecuting ? 'executing' : '',
        status === 'success' ? 'success' : '',
        status === 'error' || status === 'failed' ? 'error' : '',
    ].filter(Boolean).join(' ');

    return (
        <motion.div
            className={cls}
            data-node-type="start"
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            whileHover={{ scale: 1.035, y: -3 }}
            transition={{ type: 'spring', stiffness: 420, damping: 30, mass: 0.7 }}
        >
            <div className="node-block-body">
                <div className="node-block-icon">
                    <StartIcon />
                </div>
                <div className="node-block-text">
                    <span className="node-block-name">Start</span>
                    <span className="node-block-label">Trigger</span>
                </div>
            </div>

            <Handle
                type="source"
                position={Position.Bottom}
                id="source"
            />
        </motion.div>
    );
}

export default StartNode;
