// SPDX-License-Identifier: MIT
import { Handle, Position } from '@xyflow/react';
import { motion } from 'framer-motion';
import useWorkflowStore from '../../../../store/workflowStore';
import useAgentsStore from '../../../../store/agentsStore';
import useDashboardStore from '../../../../store/dashboardStore';
import { validateEntityName } from '../../../../utils/validateName';

// Navigation arrow — standard assistant/agent icon
const AgentIcon = () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" xmlns="http://www.w3.org/2000/svg">
        <path d="M22 2L11 13" />
        <path d="M22 2L15 22L11 13L2 9L22 2Z" />
    </svg>
);

function AgentNode({ id, data, selected }) {
    const activeNodeIds = useWorkflowStore((state) => state.activeNodeIds);
    const allNodes = useWorkflowStore((state) => state.nodes);
    const savedAgents = useAgentsStore((state) => state.agents);
    const savedWorkflows = useDashboardStore((state) => state.workflows);
    const isExecuting = activeNodeIds.includes(id);
    const status = data.status || data.executionStatus || data.state || '';

    // Live name-conflict detection.
    //
    // The node turns red whenever its current ``data.name`` collides with any
    // other entity in the user's namespace — another agent node in this
    // workflow, a saved agent, or a saved workflow. The same rule as the
    // ConfigPanel field; doing it here means the canvas reflects the conflict
    // even when the node isn't selected.
    const otherWorkflowAgentItems = (allNodes || [])
        .filter((n) => n.type === 'agent' && n.id !== id)
        .map((n) => ({ id: n.id, name: (n.data && n.data.name) || '' }));
    const savedAgentItems = (savedAgents || []).map((a) => ({
        id: `saved-agent:${a.id}`, name: a.name,
    }));
    const savedWorkflowItems = (savedWorkflows || []).map((w) => ({
        id: `saved-wf:${w.id}`, name: w.name,
    }));
    const nameError = validateEntityName(data.name || '', 'agent', {
        existingItems: [
            ...otherWorkflowAgentItems,
            ...savedAgentItems,
            ...savedWorkflowItems,
        ],
        currentId: id,
    });
    // Only treat a name *collision* as a node-level error — empty / format
    // errors are surfaced elsewhere and shouldn't paint the whole node red.
    const hasNameConflict = nameError === 'Name already in use.';
    const tools = Array.isArray(data.tools) ? data.tools : [];
    const skills = Array.isArray(data.skills) ? data.skills : [];
    const toolNames = tools.map((t) => t?.name || String(t)).filter(Boolean);
    const skillNames = skills.map((s) => s?.name || String(s)).filter(Boolean);

    const cls = [
        'node-block',
        'node-block--agent',
        selected ? 'selected' : '',
        isExecuting ? 'executing' : '',
        status === 'success' ? 'success' : '',
        status === 'error' || status === 'failed' ? 'error' : '',
        hasNameConflict ? 'node-block--name-conflict' : '',
    ].filter(Boolean).join(' ');

    return (
        <motion.div
            className={cls}
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            whileHover={{ scale: 1.035, y: -3 }}
            transition={{ type: 'spring', stiffness: 420, damping: 30, mass: 0.7 }}
        >
            <Handle type="target" position={Position.Top} id="target" />

            {/* Icon etc. */}
            <div className="node-block-body">
                <div className="node-block-icon">
                    <AgentIcon />
                </div>
                <div className="node-block-text">
                    <h4 className="node-block-name">{data.name || 'My agent'}</h4>
                    <span className="node-block-label">Agent</span>
                </div>
                {hasNameConflict && (
                    <span
                        className="node-name-conflict-badge"
                        title="Name conflicts with another agent or workflow"
                    >
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                            <line x1="12" y1="9" x2="12" y2="13" />
                            <line x1="12" y1="17" x2="12.01" y2="17" />
                        </svg>
                    </span>
                )}
                {data.hitlMode && data.hitlMode !== 'off' && (
                    <span
                        className={`node-hitl-badge node-hitl-badge--${data.hitlMode}`}
                        title={
                            data.hitlMode === 'after_response' ? 'HITL: review the final answer' :
                            data.hitlMode === 'before_tool'    ? 'HITL: review every tool call' :
                            'HITL: review tool calls and final answer'
                        }
                    >
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                            <circle cx="9" cy="7" r="4" />
                        </svg>
                    </span>
                )}
            </div>

            {/* Compact attached tools & skills summary */}
            {((toolNames.length > 0) || (skillNames.length > 0)) && (
                <div className="node-block-tags">
                    {toolNames.length > 0 && (
                        <span className="node-block-tag node-block-tag--tool" title={toolNames.join('\n')}>
                            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                                <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
                            </svg>
                            {toolNames.length} tool{toolNames.length === 1 ? '' : 's'}
                        </span>
                    )}
                    {skillNames.length > 0 && (
                        <span className="node-block-tag node-block-tag--skill" title={skillNames.join('\n')}>
                            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                                <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" />
                            </svg>
                            {skillNames.length} skill{skillNames.length === 1 ? '' : 's'}
                        </span>
                    )}
                </div>
            )}

            <Handle type="source" position={Position.Bottom} id="source" />
        </motion.div>
    );
}

export default AgentNode;
