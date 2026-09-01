// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react';
import ConfirmModal from '../../components/common/ConfirmModal';
import HoverTooltip from '../../components/common/HoverTooltip';
import useHoverTooltip from '../../hooks/useHoverTooltip';
import formatDate from '../../utils/formatDate';
import { stripProviderPrefix } from '../../utils/modelLabel';
import { stripTemplateTag } from '../../utils/templateText';
import StatusBadge from '../governance/StatusBadge';
import SubmitApprovalButton from '../governance/SubmitApprovalButton';

function AgentCard({ agent, isPreset, onClick, onDelete, onDuplicate, onTalkToAgent, className }) {
    const [showDeleteModal, setShowDeleteModal] = useState(false);
    const description = stripTemplateTag(agent.description);
    const tooltip = useHoverTooltip({ enabled: !!description });

    const handleDeleteClick = (e) => { e.stopPropagation(); setShowDeleteModal(true); };
    const handleDeleteConfirm = () => { setShowDeleteModal(false); onDelete(); };
    const handleDuplicate = (e) => { e.stopPropagation(); onDuplicate(); };
    const handleTalkToAgent = (e) => { e.stopPropagation(); onTalkToAgent(agent); };

    const modelLabel = stripProviderPrefix(agent.model_name);

    // Tools / skills attached by the Agent Factory. Presets don't have any.
    const tools  = Array.isArray(agent.tools)  ? agent.tools  : [];
    const skills = Array.isArray(agent.skills) ? agent.skills : [];
    const hasCapabilities = tools.length > 0 || skills.length > 0;

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
                <div className="workflow-card-header">
                    {isPreset ? (
                        <div className="workflow-card-icon">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M12 2 L13.5 10.5 L22 12 L13.5 13.5 L12 22 L10.5 13.5 L2 12 L10.5 10.5 Z" />
                            </svg>
                        </div>
                    ) : (
                        <div className="workflow-card-icon agent-card-icon">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M12 2a4 4 0 0 1 4 4 4 4 0 0 1-4 4 4 4 0 0 1-4-4 4 4 0 0 1 4-4m0 10c4.42 0 8 1.79 8 4v2H4v-2c0-2.21 3.58-4 8-4z" />
                            </svg>
                        </div>
                    )}
                    <div className="workflow-card-title-block">
                        <div className="workflow-card-name-row">
                            <h3 className="workflow-card-name">{agent.name}</h3>
                            {!isPreset && (
                                <span className="workflow-status-badge status-agent">
                                    <span className="status-dot" />
                                    Agent
                                </span>
                            )}
                            {/* Governance approval status (additive). */}
                            {!isPreset && agent.name && (
                                <StatusBadge entityType="agents" name={agent.name} poll={false} />
                            )}
                            {/* Preset visibility classification — shown on every
                                preset card so users see who it's for. */}
                            {isPreset && (
                                agent.visibility === 'private' ? (
                                    <span
                                        className="workflow-status-badge"
                                        title={agent.department
                                            ? `Visible to the ${agent.department} department only`
                                            : 'Department-only preset'}
                                        style={{ background: '#f5f3ff', color: '#6d28d9', border: '1px solid #ddd6fe' }}
                                    >
                                        {agent.department || 'Department'}
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
                        {isPreset && description && (
                            <p className="workflow-card-description">{description}</p>
                        )}
                    </div>
                </div>

                {!isPreset && (hasCapabilities || agent.name) && (
                    <div className="agent-card-cap-row">
                        <div className="agent-card-cap-summary">
                            {tools.length > 0 && (
                                <span className="agent-cap-pill agent-cap-pill--tool">
                                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                                        <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
                                    </svg>
                                    {tools.length} {tools.length === 1 ? 'tool' : 'tools'}
                                </span>
                            )}
                            {skills.length > 0 && (
                                <span className="agent-cap-pill agent-cap-pill--skill">
                                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                                        <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
                                    </svg>
                                    {skills.length} {skills.length === 1 ? 'skill' : 'skills'}
                                </span>
                            )}
                        </div>
                        {/* Submit-for-approval affordance — hidden once pending/approved. */}
                        <div onClick={e => e.stopPropagation()}>
                            <SubmitApprovalButton entityType="agents" name={agent.name} />
                        </div>
                    </div>
                )}

                <div className="workflow-card-footer">
                    <div className="workflow-card-meta">
                        {isPreset && agent.category && (
                            <span className="workflow-card-category">{agent.category}</span>
                        )}
                        {!isPreset && modelLabel && (
                            <span className="workflow-card-stat">
                                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                    <rect x="2" y="3" width="20" height="14" rx="2" />
                                    <path d="M8 21h8M12 17v4" />
                                </svg>
                                {modelLabel}
                            </span>
                        )}
                        {!isPreset && agent.updated_at && (
                            <span className="workflow-card-date">{formatDate(agent.updated_at)}</span>
                        )}
                    </div>

                    {isPreset && (
                        <span className="workflow-use-template-hint">Use template →</span>
                    )}
                    {!isPreset && <div className="workflow-card-actions" onClick={e => e.stopPropagation()}>
                        {onTalkToAgent && (
                            <button
                                className="card-action-btn card-action-talk"
                                title="Talk to Agent"
                                onClick={handleTalkToAgent}
                                aria-label="Talk to agent"
                            >
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                                </svg>
                            </button>
                        )}
                        {onDuplicate && (
                            <button className="card-action-btn" title="Duplicate" onClick={handleDuplicate} aria-label="Duplicate agent">
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                                </svg>
                            </button>
                        )}
                        {onDelete && (
                            <button className="card-action-btn card-action-delete" title="Delete" onClick={handleDeleteClick} aria-label="Delete agent">
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                    <polyline points="3 6 5 6 21 6" />
                                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                                </svg>
                            </button>
                        )}
                    </div>}
                </div>

                <HoverTooltip
                    id={tooltip.tooltipId}
                    placement={tooltip.placement}
                    visible={tooltip.visible}
                    title={agent.name}
                    body={description}
                />
            </div>

            <ConfirmModal
                isOpen={showDeleteModal}
                title="Delete Agent"
                message={`Are you sure you want to delete "${agent.name}"? This action cannot be undone.`}
                onConfirm={handleDeleteConfirm}
                onCancel={() => setShowDeleteModal(false)}
                confirmText="Delete"
                confirmStyle="danger"
            />
        </>
    );
}

export default AgentCard;
