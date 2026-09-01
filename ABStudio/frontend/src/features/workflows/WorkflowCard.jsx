// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react';
import ConfirmModal from '../../components/common/ConfirmModal';
import HoverTooltip from '../../components/common/HoverTooltip';
import useHoverTooltip from '../../hooks/useHoverTooltip';
import formatDate from '../../utils/formatDate';
import { stripTemplateTag } from '../../utils/templateText';
import StatusBadge from '../governance/StatusBadge';
import SubmitApprovalButton from '../governance/SubmitApprovalButton';

function WorkflowCard({ workflow, isTemplate, onClick, onDelete, onDuplicate, onTalkToAgent, onTrigger, className }) {
    const [showDeleteModal, setShowDeleteModal] = useState(false);
    const description = stripTemplateTag(workflow.description);
    const tooltip = useHoverTooltip({ enabled: !!description });

    const getNodeStats = () => {
        try {
            const graphData = workflow.graph_data || workflow.graphData;
            if (!graphData?.nodes) return { agents: 0, total: 0 };
            const nodes = graphData.nodes;
            const agents = nodes.filter(n => n.type === 'agent').length;
            const total = nodes.filter(n => n.type !== 'start' && n.type !== 'end').length;
            return { agents, total };
        } catch {
            return { agents: 0, total: 0 };
        }
    };

    const stats = getNodeStats();
    const status = workflow.status || 'draft';
    const statusConfig = {
        draft: { label: 'Draft', cls: 'status-draft' },
        active: { label: 'Active', cls: 'status-active' },
        failed: { label: 'Failed', cls: 'status-failed' },
    };
    const { label: statusLabel, cls: statusCls } = statusConfig[status] ?? statusConfig.draft;

    const handleDeleteClick = (e) => { e.stopPropagation(); setShowDeleteModal(true); };
    const handleDeleteConfirm = () => { setShowDeleteModal(false); onDelete(); };
    const handleDuplicate = (e) => { e.stopPropagation(); onDuplicate(); };
    const handleTalkToAgent = (e) => { e.stopPropagation(); onTalkToAgent(workflow); };

    return (
        <>
            <div
                {...tooltip.anchorProps}
                className={`workflow-card${className ? ` ${className}` : ''}`}
                onClick={onClick}
                tabIndex={0}
                role="button"
                onKeyDown={e => e.key === 'Enter' && onClick()}
            >
                {/* Header: icon + title + status badge */}
                <div className="workflow-card-header">
                    <div className="workflow-card-icon">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                            <path d="M12 2 L13.5 10.5 L22 12 L13.5 13.5 L12 22 L10.5 13.5 L2 12 L10.5 10.5 Z" />
                        </svg>
                    </div>
                    <div className="workflow-card-title-block">
                        <div className="workflow-card-name-row">
                            <h3 className="workflow-card-name">{workflow.name}</h3>
                            {!isTemplate && (
                                <span className={`workflow-status-badge ${statusCls}`}>
                                    <span className="status-dot" />
                                    {statusLabel}
                                </span>
                            )}
                            {/* Governance approval status (additive — does not
                                replace the existing draft/active badge). */}
                            {!isTemplate && workflow.name && (
                                <StatusBadge entityType="workflows" name={workflow.name} poll={false} />
                            )}
                            {/* Template visibility classification — shown on
                                every template card so users see who it's for. */}
                            {isTemplate && (
                                workflow.visibility === 'private' ? (
                                    <span
                                        className="workflow-status-badge"
                                        title={workflow.department
                                            ? `Visible to the ${workflow.department} department only`
                                            : 'Department-only template'}
                                        style={{ background: '#f5f3ff', color: '#6d28d9', border: '1px solid #ddd6fe' }}
                                    >
                                        {workflow.department || 'Department'}
                                    </span>
                                ) : (
                                    <span
                                        className="workflow-status-badge"
                                        title="Visible to all users"
                                        style={{ background: '#dcfce7', color: '#15803d', border: '1px solid #bbf7d0' }}
                                    >
                                        Public
                                    </span>
                                )
                            )}
                        </div>
                        {description && (
                            <p className="workflow-card-description">{description}</p>
                        )}
                    </div>
                </div>

                {/* Middle row: agent count pill + Deploy — mirrors AgentCard's cap-row */}
                {!isTemplate && (stats.agents > 0 || workflow.name) && (
                    <div className="agent-card-cap-row">
                        <div className="agent-card-cap-summary">
                            {stats.agents > 0 && (
                                <span className="agent-cap-pill agent-cap-pill--tool">
                                    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor">
                                        <path d="M12 12c2.2 0 4-1.8 4-4s-1.8-4-4-4-4 1.8-4 4 1.8 4 4 4zm0 2c-2.7 0-8 1.3-8 4v2h16v-2c0-2.7-5.3-4-8-4z" />
                                    </svg>
                                    {stats.agents} agent{stats.agents !== 1 ? 's' : ''}
                                </span>
                            )}
                        </div>
                        <div onClick={e => e.stopPropagation()}>
                            <SubmitApprovalButton entityType="workflows" name={workflow.name} />
                        </div>
                    </div>
                )}

                {/* Footer: date + icon buttons */}
                <div className="workflow-card-footer">
                    <div className="workflow-card-meta">
                        {isTemplate && workflow.category && (
                            <span className="workflow-card-category">{workflow.category}</span>
                        )}
                        {!isTemplate && workflow.updated_at && (
                            <span className="workflow-card-date">{formatDate(workflow.updated_at)}</span>
                        )}
                    </div>

                    {!isTemplate && (
                        <div className="workflow-card-actions" onClick={e => e.stopPropagation()}>
                            {onTrigger && (
                                <button
                                    className="card-action-btn"
                                    title="Schedule / Trigger"
                                    onClick={e => { e.stopPropagation(); onTrigger(workflow); }}
                                    aria-label="Schedule workflow"
                                >
                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                        <circle cx="12" cy="12" r="10" />
                                        <polyline points="12 6 12 12 16 14" />
                                    </svg>
                                </button>
                            )}
                            {onDuplicate && (
                                <button className="card-action-btn" title="Duplicate" onClick={handleDuplicate} aria-label="Duplicate workflow">
                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                        <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                                        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                                    </svg>
                                </button>
                            )}
                            {onDelete && (
                                <button className="card-action-btn card-action-delete" title="Delete" onClick={handleDeleteClick} aria-label="Delete workflow">
                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                        <polyline points="3 6 5 6 21 6" />
                                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                                    </svg>
                                </button>
                            )}
                        </div>
                    )}
                    {isTemplate && (
                        <span className="workflow-use-template-hint">Use template →</span>
                    )}
                </div>

                <HoverTooltip
                    id={tooltip.tooltipId}
                    placement={tooltip.placement}
                    visible={tooltip.visible}
                    title={workflow.name}
                    body={description}
                />
            </div>

            <ConfirmModal
                isOpen={showDeleteModal}
                title="Delete Workflow"
                message={`Are you sure you want to delete "${workflow.name}"? This action cannot be undone.`}
                onConfirm={handleDeleteConfirm}
                onCancel={() => setShowDeleteModal(false)}
                confirmText="Delete"
                confirmStyle="danger"
            />
        </>
    );
}

export default WorkflowCard;
