// SPDX-License-Identifier: Apache-2.0
import { useState, useRef, useEffect } from 'react';
import { BaseEdge, EdgeLabelRenderer, getBezierPath } from '@xyflow/react';
import useWorkflowStore from '../../../../store/workflowStore';

// Node types available for insertion (NOT start/end — those are singletons)
const INSERTABLE_TYPES = [
    { type: 'agent', label: 'Agent' },
    { type: 'condition', label: 'Condition' },
    { type: 'loop', label: 'Loop' },
    { type: 'subflow', label: 'Existing Asset' },
];

function AiEdge({
    id,
    source,
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
}) {
    const [edgePath, labelX, labelY] = getBezierPath({
        sourceX,
        sourceY,
        sourcePosition,
        targetX,
        targetY,
        targetPosition,
        curvature: 0.34,
    });

    const insertNodeOnEdge = useWorkflowStore((s) => s.insertNodeOnEdge);
    const removeEdge = useWorkflowStore((s) => s.removeEdge);
    const hoveredEdgeId = useWorkflowStore((s) => s.hoveredEdgeId);
    const activeNodeIds = useWorkflowStore((s) => s.activeNodeIds);
    const isHovered = hoveredEdgeId === id;

    // Animate the edge when its source node is actively executing
    const isAnimated = activeNodeIds.includes(source);

    const [menuOpen, setMenuOpen] = useState(false);
    const menuRef = useRef(null);

    // Close menu on outside click
    useEffect(() => {
        if (!menuOpen) return;
        const handler = (e) => {
            if (menuRef.current && !menuRef.current.contains(e.target)) {
                setMenuOpen(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [menuOpen]);

    const handleInsert = (nodeType) => {
        // Place the new node at the edge midpoint, offset left by half
        // a typical node width (~80px) so it centers visually.
        insertNodeOnEdge(id, nodeType, { x: labelX - 80, y: labelY - 30 });
        setMenuOpen(false);
    };

    const handleDelete = (e) => {
        e.stopPropagation();
        removeEdge(id);
    };

    return (
        <>
            <BaseEdge
                id={id}
                path={edgePath}
                className={`ai-workflow-edge${isAnimated ? ' animated' : ''}`}
            />

            <EdgeLabelRenderer>
                <div
                    className="edge-insert-wrapper"
                    style={{
                        position: 'absolute',
                        transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
                        pointerEvents: 'all',
                    }}
                >
                    <div className={`edge-action-btns ${isHovered || menuOpen ? 'edge-action-btns--visible' : ''}`}>
                        {/* Insert node button */}
                        <button
                            className="edge-insert-btn"
                            onClick={(e) => {
                                e.stopPropagation();
                                if (INSERTABLE_TYPES.length === 1) {
                                    handleInsert(INSERTABLE_TYPES[0].type);
                                } else {
                                    setMenuOpen((prev) => !prev);
                                }
                            }}
                            title="Insert node"
                        >
                            +
                        </button>

                        {/* Delete edge button */}
                        <button
                            className="edge-delete-btn"
                            onClick={handleDelete}
                            title="Delete connection"
                        >
                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round">
                                <path d="M18 6L6 18M6 6l12 12" />
                            </svg>
                        </button>
                    </div>

                    {menuOpen && INSERTABLE_TYPES.length > 1 && (
                        <div className="edge-insert-menu" ref={menuRef}>
                            {INSERTABLE_TYPES.map((item) => (
                                <button
                                    key={item.type}
                                    className="edge-insert-menu-item"
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        handleInsert(item.type);
                                    }}
                                >
                                    {item.label}
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            </EdgeLabelRenderer>
        </>
    );
}

export default AiEdge;
