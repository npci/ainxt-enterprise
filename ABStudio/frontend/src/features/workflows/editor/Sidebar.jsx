// SPDX-License-Identifier: Apache-2.0
import useWorkflowStore from '../../../store/workflowStore';

function Sidebar() {
    const nodes = useWorkflowStore((state) => state.nodes);

    const hasEndNode = nodes.some((n) => n.type === 'end');

    const onDragStart = (event, nodeType) => {
        event.dataTransfer.setData('application/reactflow', nodeType);
        event.dataTransfer.effectAllowed = 'move';
    };

    return (
        <div className="sidebar">
            <h3 className="sidebar-title">Node Palette</h3>
            <div className="node-palette">
                <div
                    className="draggable-node"
                    onDragStart={(e) => onDragStart(e, 'agent')}
                    draggable
                >
                    <div className="node-icon agent">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M22 2L11 13" />
                            <path d="M22 2L15 22L11 13L2 9L22 2Z" />
                        </svg>
                    </div>
                    <span className="node-label">Agent</span>
                </div>

                <div
                    className="draggable-node"
                    onDragStart={(e) => onDragStart(e, 'condition')}
                    draggable
                >
                    <div className="node-icon condition">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M12 2L22 12L12 22L2 12L12 2Z" />
                            <path d="M9 12L11 14L15 10" />
                        </svg>
                    </div>
                    <span className="node-label">Condition</span>
                </div>

                <div
                    className="draggable-node"
                    onDragStart={(e) => onDragStart(e, 'subflow')}
                    draggable
                    title="Drop and link to an existing workflow or agent"
                >
                    <div className="node-icon subflow">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                            <rect x="3" y="3" width="13" height="13" rx="2" />
                            <rect x="8" y="8" width="13" height="13" rx="2" />
                        </svg>
                    </div>
                    <span className="node-label">Existing Asset</span>
                </div>

                <div
                    className="draggable-node"
                    onDragStart={(e) => onDragStart(e, 'loop')}
                    draggable
                    title="Iterate the body subgraph until termination"
                >
                    <div className="node-icon loop">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M17 2l4 4-4 4" />
                            <path d="M3 11v-1a4 4 0 0 1 4-4h14" />
                            <path d="M7 22l-4-4 4-4" />
                            <path d="M21 13v1a4 4 0 0 1-4 4H3" />
                        </svg>
                    </div>
                    <span className="node-label">Loop</span>
                </div>

                <div
                    className={`draggable-node ${hasEndNode ? 'disabled' : ''}`}
                    onDragStart={(e) => !hasEndNode && onDragStart(e, 'end')}
                    draggable={!hasEndNode}
                    title={hasEndNode ? 'Only one End node allowed' : 'End node'}
                >
                    <div className="node-icon end">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg">
                            <rect x="6" y="6" width="12" height="12" rx="2" />
                        </svg>
                    </div>
                    <span className="node-label">End</span>
                </div>
            </div>

        </div>
    );
}

export default Sidebar;
