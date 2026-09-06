// SPDX-License-Identifier: MIT
import { useState, useCallback, useEffect, useRef, useMemo, Fragment } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion as fm } from 'framer-motion';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { API_BASE, buildAuthHeaders } from '../../config/api';
import CatalogPicker from '../../components/common/CatalogPicker';
import KnowledgeSection, { KB_MODE_NONE } from '../../components/common/KnowledgeSection';
import TriggerSection from '../triggers/TriggerSection';
import TRIGGER_SCOPED_CSS from '../triggers/triggerScopedCss';
import { sniffGeneratedFiles } from '../_shared/sniffGeneratedFiles';
import FactoryFileChips, { absoluteDownloadUrl } from '../_shared/FactoryFileChips';
import { downloadGeneratedFile } from '../_shared/downloadGeneratedFile';
import { useTransientNotice } from '../_shared/useTransientNotice';
import DownloadNotice from '../_shared/DownloadNotice';
import { useFactoryChatStream } from '../_shared/useFactoryChatStream';
import PlanCard from '../../components/common/PlanCard';
import AnswerCards from '../../components/common/AnswerCards';
import { useTriggerPortalContainer } from '../triggers/triggerPortal';
import * as base from '../../styles/chatOverlayStyles';
import { autoGrowTextarea, motion as motionTokens } from '../../styles/chatOverlayStyles';
import { stripEmoji } from '../../utils/stripEmoji';
import useAvailableModels, { MODEL_STATUS } from '../../hooks/useAvailableModels';
import { stripProviderPrefix } from '../../utils/modelLabel';
import { getMaxTokensForModel } from '../../utils/modelMaxTokens';
import useCurrentUser from '../../hooks/useCurrentUser';

const SPARK_ICON = (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 2L13.5 10.5L22 12L13.5 13.5L12 22L10.5 13.5L2 12L10.5 10.5Z" />
    </svg>
);

// Defaults mirror agent_factory.pipeline._apply_domain_defaults so the config
// panel never renders "undefined" before the first ``assembled`` arrives.
const DEFAULT_MODEL_PARAMS = { temperature: 0.3, max_tokens: 4096, top_p: 0.9 };
const DEFAULT_KNOWLEDGE = { mode: KB_MODE_NONE, namespaces: [], suggested_topics: [], reason: '' };
const HITL_MODES = [
    { value: 'off',             label: 'Off — no human review' },
    { value: 'after_response',  label: 'After response — review drafts before they go out' },
    { value: 'before_tool',     label: 'Before tool — approve every external action' },
];
const REMARK_PLUGINS = [remarkGfm];
const FOCUSABLE =
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Same shape helper the AgentEditor uses for its model dropdown. Duplicated
// here (rather than exported from AgentEditor) so this file has no dependency
// on the manual editor's internals.
function AgentModelPicker({ value, models, providers, defaultModel, status, onChange }) {
    const [open, setOpen] = useState(false);
    const wrapperRef = useRef(null);

    const groups = useMemo(() => {
        if (Array.isArray(providers) && providers.length > 0) {
            return providers
                .map(g => ({
                    label: g.provider || '',
                    options: (g.models || [])
                        .map(m => (typeof m === 'string'
                            ? { id: m, label: stripProviderPrefix(m) }
                            : { id: m.id, label: m.label || stripProviderPrefix(m.id) }))
                        .filter(o => o.id),
                }))
                .filter(s => s.options.length > 0);
        }
        const flat = (models && models.length ? models : [value || defaultModel])
            .filter(Boolean)
            .filter((m, i, arr) => arr.indexOf(m) === i);
        return flat.length > 0
            ? [{ label: '', options: flat.map(id => ({ id, label: stripProviderPrefix(id) })) }]
            : [];
    }, [providers, models, value, defaultModel]);

    const flatIds = groups.flatMap(g => g.options.map(o => o.id));
    const selected = value || defaultModel || flatIds[0] || '';

    useEffect(() => {
        if (!open) return undefined;
        const onMouseDown = (event) => {
            if (!wrapperRef.current?.contains(event.target)) setOpen(false);
        };
        document.addEventListener('mousedown', onMouseDown);
        return () => document.removeEventListener('mousedown', onMouseDown);
    }, [open]);

    useEffect(() => {
        if (!open) return undefined;
        const onKey = (event) => { if (event.key === 'Escape') setOpen(false); };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [open]);

    return (
        <div className={`agent-model-inline${open ? ' open' : ''}`} ref={wrapperRef}>
            <button
                type="button"
                className="agent-model-inline-trigger"
                onClick={() => setOpen(v => !v)}
                disabled={status === MODEL_STATUS.LOADING && flatIds.length === 0}
                aria-haspopup="listbox"
                aria-expanded={open}
            >
                <span className="agent-model-inline-value">{stripProviderPrefix(selected)}</span>
                <svg className="agent-model-inline-chevron" width="16" height="16" viewBox="0 0 24 24"
                     fill="none" stroke="currentColor" strokeWidth="2"
                     strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <polyline points="6 9 12 15 18 9" />
                </svg>
            </button>
            {open && (
                <div className="agent-model-inline-list" role="listbox">
                    {groups.map((group, i) => (
                        <Fragment key={group.label || `flat-${i}`}>
                            {group.label && (
                                <div className="agent-model-inline-group" role="presentation">
                                    {group.label}
                                </div>
                            )}
                            {group.options.map(option => {
                                const active = option.id === selected;
                                return (
                                    <button
                                        key={option.id}
                                        type="button"
                                        className={`agent-model-inline-option${active ? ' selected' : ''}`}
                                        onClick={() => { onChange(option.id); setOpen(false); }}
                                        role="option"
                                        aria-selected={active}
                                    >
                                        {option.label}
                                    </button>
                                );
                            })}
                        </Fragment>
                    ))}
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// AgentFactoryChat — two-pane "Create with AI" modal.
//
//   Left pane:  the SSE-driven chat (matches WorkflowFactoryChat / SkillFactoryChat).
//   Right pane: the SAME configuration form the manual AgentEditor renders,
//               driven by local state so it can be edited manually AT ANY TIME.
//
// The chat can also drive targeted edits — a user saying "make the system
// prompt more aggressive" causes the backend to return a patch that touches
// only ``system_prompt``. All other fields stay exactly as the user last
// left them (manual OR AI). The agent is NOT persisted until the user hits
// **Deploy Agent** in the right-pane header.
// ---------------------------------------------------------------------------
function AgentFactoryChat({ onClose, onDeployed }) {
    const portalContainer = useTriggerPortalContainer();
    const panelRef = useRef(null);
    const inputRef = useRef(null);
    const messagesEndRef = useRef(null);
    const previouslyFocused = useRef(null);
    const titleId = useRef(`factory-chat-title-${Math.floor(Math.random() * 1e6)}`).current;

    const currentUser = useCurrentUser();
    const {
        models: availableModels,
        providers: availableProviders,
        defaultModel,
        status: modelsStatus,
        error: modelsError,
    } = useAvailableModels();

    // ── Runtime config surfaced in the right-pane (all fields editable) ──
    const [assembledAgent, setAssembledAgent] = useState(null);
    const [pendingTools, setPendingTools] = useState([]);
    const [pendingSkills, setPendingSkills] = useState([]);
    const [pendingModelName, setPendingModelName] = useState('');
    const [pendingParams, setPendingParams] = useState(DEFAULT_MODEL_PARAMS);
    const [pendingKnowledge, setPendingKnowledge] = useState(DEFAULT_KNOWLEDGE);
    const [pendingHitl, setPendingHitl] = useState('off');
    const [pendingName, setPendingName] = useState('');
    const [pendingDescription, setPendingDescription] = useState('');
    const [pendingSystemPrompt, setPendingSystemPrompt] = useState('');

    // ── Deploy state ──
    const [deployedAgent, setDeployedAgent] = useState(null);
    const [isDeploying, setIsDeploying] = useState(false);
    const [deployError, setDeployError] = useState('');
    const [scheduled, setScheduled] = useState(false);

    // ── Chat / SSE plumbing ──
    const [inputValue, setInputValue] = useState('');
    const [existingMatches, setExistingMatches] = useState([]);
    const [openingMatchId, setOpeningMatchId] = useState(null);
    const [matchError, setMatchError] = useState('');
    const [planCard, setPlanCard] = useState(null);

    // The extra body we send on every /agent-factory/chat call so the backend
    // patcher sees the user's live view of the config, not the last blueprint.
    // Held in a ref so ``useCallback`` doesn't churn on every keystroke.
    const currentAssembledRef = useRef({});
    useEffect(() => {
        currentAssembledRef.current = {
            name: pendingName,
            description: pendingDescription,
            system_prompt: pendingSystemPrompt,
            model: pendingModelName,
            model_params: pendingParams,
            knowledge: {
                mode: pendingKnowledge.mode,
                namespaces: pendingKnowledge.namespaces || [],
            },
            hitl_mode: pendingHitl,
            // Tools & skills are not chat-patchable, but the backend mirrors
            // them so a manual add/remove survives the next SSE round trip.
            tools: pendingTools,
            skills: pendingSkills,
        };
    }, [pendingName, pendingDescription, pendingSystemPrompt, pendingModelName,
        pendingParams, pendingKnowledge, pendingHitl, pendingTools, pendingSkills]);

    const buildExtraBody = useCallback(() => {
        // Only send the override once we have an assembled agent — before that
        // the backend still needs to drive the clarification → plan-card path.
        if (!assembledAgent) return {};
        return { current_assembled_override: currentAssembledRef.current };
    }, [assembledAgent]);

    const onMessage = useCallback((ev) => {
        if (ev.data?.assembled) {
            setAssembledAgent(ev.data.assembled);
        }
        if (ev.stage === 'plan_card') {
            setPlanCard(ev.data?.plan_card ?? null);
        } else if (ev.stage) {
            setPlanCard(null);
        }
        if (ev.stage === 'suggest_existing') {
            setExistingMatches(ev.data?.existing_matches || []);
        } else if (ev.data?.existing_matches === undefined) {
            setExistingMatches([]);
        }
    }, []);

    const onReset = useCallback(() => {
        setDeployError('');
        setExistingMatches([]);
        setMatchError('');
        setPlanCard(null);
    }, []);

    const {
        messages, suggestions, stage, isLoading, sessionId, sendMessage,
    } = useFactoryChatStream({
        endpoint: '/agent-factory/chat',
        onMessage,
        onReset,
        buildExtraBody,
    });

    const [downloadNotice, setDownloadNotice] = useTransientNotice();
    const handleDownloadGenerated = useCallback(async (file) => {
        const result = await downloadGeneratedFile(absoluteDownloadUrl(file), file.filename);
        if (result.status !== 'ok') setDownloadNotice({ kind: result.status, text: result.message });
    }, [setDownloadNotice]);

    // Whenever the backend emits a new ``assembled`` (initial build OR a patch
    // response), sync the parts of it we haven't necessarily patched into the
    // local editable state. This keeps ``tools`` / ``skills`` / other blueprint
    // fields in sync while ALSO respecting fields the user tweaked manually —
    // the backend already carries those forward through ``current_assembled_override``.
    useEffect(() => {
        if (!assembledAgent) return;
        setPendingTools(assembledAgent.tools || []);
        setPendingSkills(assembledAgent.skills || []);
        setPendingModelName(assembledAgent.model || '');
        setPendingParams({
            temperature: assembledAgent.model_params?.temperature ?? DEFAULT_MODEL_PARAMS.temperature,
            max_tokens:  assembledAgent.model_params?.max_tokens  ?? DEFAULT_MODEL_PARAMS.max_tokens,
            top_p:       assembledAgent.model_params?.top_p       ?? DEFAULT_MODEL_PARAMS.top_p,
        });
        setPendingKnowledge({
            mode:              assembledAgent.knowledge?.mode || KB_MODE_NONE,
            namespaces:        assembledAgent.knowledge?.namespaces || [],
            suggested_topics:  assembledAgent.knowledge?.suggested_topics || [],
            reason:            assembledAgent.knowledge?.reason || '',
        });
        setPendingHitl(assembledAgent.hitl_mode || 'off');
        setPendingName(assembledAgent.name || '');
        setPendingDescription(assembledAgent.description || '');
        setPendingSystemPrompt(assembledAgent.system_prompt || '');
    }, [assembledAgent]);

    const handleSend = (text) => {
        setInputValue('');
        sendMessage(text);
    };

    const handleChipClick = (text) => {
        if (!text || isLoading) return;
        sendMessage(text);
    };

    const handleOpenExisting = async (match) => {
        if (!match?.id || openingMatchId) return;
        setOpeningMatchId(match.id);
        setMatchError('');
        try {
            const res = await fetch(`${API_BASE}/agent-templates/${match.id}/use`, {
                method: 'POST',
                headers: buildAuthHeaders(),
            });
            const agent = await res.json();
            if (!res.ok) throw new Error(agent.detail || 'Could not use template');
            onDeployed(agent);
        } catch (err) {
            setMatchError(err.message);
            setOpeningMatchId(null);
        }
    };

    const handleBuildAnyway = () => {
        if (isLoading) return;
        handleSend("None of these fit — let's continue building a new agent.");
    };

    const handleDeploy = useCallback(async () => {
        if (!sessionId || isDeploying || !assembledAgent) return;
        setIsDeploying(true);
        setDeployError('');
        try {
            const res = await fetch(`${API_BASE}/agent-factory/confirm`, {
                method: 'POST',
                headers: buildAuthHeaders(),
                body: JSON.stringify({
                    session_id: sessionId,
                    tools_override: pendingTools,
                    skills_override: pendingSkills,
                    model_name_override:    pendingModelName || undefined,
                    model_params_override:  pendingParams,
                    knowledge_override:     pendingKnowledge,
                    hitl_mode_override:     pendingHitl,
                    name_override:          pendingName || undefined,
                    description_override:   pendingDescription,
                    system_prompt_override: pendingSystemPrompt || undefined,
                }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Deployment failed');
            setDeployedAgent({
                agent_id: data.agent_id || data.id,
                name: pendingName || assembledAgent?.name || 'Agent',
                raw: data,
            });
            setScheduled(true);
        } catch (err) {
            setDeployError(err.message);
        } finally {
            setIsDeploying(false);
        }
    }, [sessionId, isDeploying, assembledAgent, pendingTools, pendingSkills,
        pendingModelName, pendingParams, pendingKnowledge, pendingHitl,
        pendingName, pendingDescription, pendingSystemPrompt]);

    const handleFinishScheduled = () => {
        if (deployedAgent) onDeployed(deployedAgent.raw);
        else onClose();
    };

    // ── Focus trap + Escape + focus restore (mirrors FactoryChatShell) ──
    useEffect(() => {
        previouslyFocused.current = document.activeElement;
        const t = setTimeout(() => {
            (inputRef.current || panelRef.current)?.focus?.();
        }, 40);
        return () => {
            clearTimeout(t);
            previouslyFocused.current?.focus?.();
        };
    }, []);

    useEffect(() => {
        if (!inputValue && inputRef.current) {
            inputRef.current.style.height = '38px';
        }
    }, [inputValue]);

    const onKeyDown = useCallback((e) => {
        if (e.key === 'Escape') { e.stopPropagation(); onClose?.(); return; }
        if (e.key !== 'Tab') return;
        const panel = panelRef.current;
        if (!panel) return;
        const nodes = Array.from(panel.querySelectorAll(FOCUSABLE))
            .filter((n) => n.offsetParent !== null || n === document.activeElement);
        if (nodes.length === 0) return;
        const first = nodes[0]; const last = nodes[nodes.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }, [onClose]);

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({
            behavior: isLoading ? 'auto' : 'smooth',
            block: 'end',
        });
    }, [messages, suggestions, isLoading]);

    const handleTextareaKey = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            const text = inputValue.trim();
            if (text && !isLoading) handleSend(text);
        }
    };

    const handleSendClick = () => {
        const text = inputValue.trim();
        if (text && !isLoading) handleSend(text);
    };

    if (!portalContainer) return null;

    const isConfirm = stage === 'confirm' && !!assembledAgent;
    const isScheduling = scheduled;

    // Right pane content — either the deploy-success trigger panel or the
    // full editable configuration form.
    const rightPane = isScheduling && deployedAgent ? (
        <div className="agent-factory-trigger-panel factory-trigger-root" style={S.triggerPanel}>
            <style>{TRIGGER_SCOPED_CSS}</style>
            <div className="agent-factory-trigger-success" style={S.triggerSuccess}>
                <div className="agent-factory-trigger-success-icon" style={S.triggerSuccessIcon}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round">
                        <polyline points="20 6 9 17 4 12" />
                    </svg>
                </div>
                <div style={{ minWidth: 0 }}>
                    <div className="agent-factory-trigger-success-title" style={S.triggerSuccessTitle}>
                        Deployed — <strong>{deployedAgent.name}</strong>
                    </div>
                    <div className="agent-factory-trigger-success-sub" style={S.triggerSuccessSub}>
                        Add a trigger to run it automatically, or click <em style={{ color: '#4f46e5', fontStyle: 'normal', fontWeight: 600 }}>Finish</em> to skip.
                    </div>
                </div>
            </div>
            <div className="agent-factory-trigger-scroll" style={S.triggerScroll}>
                <TriggerSection targetKind="agent" targetId={deployedAgent.agent_id} variant="card" />
            </div>
            <div className="agent-factory-trigger-actions" style={S.triggerActions}>
                <button type="button" className="agent-factory-trigger-finish" style={S.triggerFinishBtn} onClick={handleFinishScheduled}>
                    Finish
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" style={{ marginLeft: 6 }}>
                        <line x1="5" y1="12" x2="19" y2="12" /><polyline points="12 5 19 12 12 19" />
                    </svg>
                </button>
            </div>
        </div>
    ) : (
        <div style={S.configPane}>
            <div style={S.configHeader}>
                <div style={{ minWidth: 0 }}>
                    <div style={S.configHeaderTitle}>Agent configuration</div>
                    <div style={S.configHeaderSub}>
                        {isConfirm
                            ? 'Preview and tweak any field — the chat can also update fields for you.'
                            : 'The chat will fill this in as we go. You can also start editing directly.'}
                    </div>
                </div>
                <button
                    type="button"
                    style={S.deployBtn(!isConfirm || isDeploying)}
                    onClick={handleDeploy}
                    disabled={!isConfirm || isDeploying}
                    title={!isConfirm ? 'Finish the setup chat first' : 'Deploy this agent'}
                >
                    {isDeploying ? (
                        <><div style={S.btnSpinner} />Deploying…</>
                    ) : (
                        <>
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                <polygon points="5 3 19 12 5 21 5 3" />
                            </svg>
                            Deploy Agent
                        </>
                    )}
                </button>
            </div>

            {deployError && (
                <div style={S.configError}>{deployError}</div>
            )}

            {/* Shares the exact CSS class hierarchy used by the manual
                AgentEditor's edit view so the visual is identical. */}
            <div className="agent-config-scroll" style={S.configScroll}>
                <div className="agent-config-form">
                    <div className="agent-config-section">
                        <h2 className="agent-config-section-title">General</h2>

                        <div className="agent-field">
                            <label className="agent-field-label">Name</label>
                            <input
                                type="text"
                                className="agent-field-input"
                                placeholder="Agent name"
                                value={pendingName}
                                onChange={(e) => setPendingName(e.target.value)}
                            />
                        </div>

                        <div className="agent-field">
                            <label className="agent-field-label">Description</label>
                            <input
                                type="text"
                                className="agent-field-input"
                                placeholder="What does this agent do?"
                                value={pendingDescription}
                                onChange={(e) => setPendingDescription(e.target.value)}
                            />
                        </div>

                        <div className="agent-field">
                            <label className="agent-field-label">
                                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                                    Instructions
                                    <span className="agent-field-hint">System prompt — defines the agent's behavior and persona</span>
                                </span>
                            </label>
                            <textarea
                                className="agent-field-textarea"
                                placeholder="You are a helpful assistant. Your goal is to…"
                                value={pendingSystemPrompt}
                                onChange={(e) => setPendingSystemPrompt(e.target.value)}
                                rows={10}
                            />
                        </div>
                    </div>

                    <div className="agent-config-section">
                        <h2 className="agent-config-section-title">Model Configuration</h2>
                        <div className="agent-field">
                            <label className="agent-field-label">Model</label>
                            <AgentModelPicker
                                value={pendingModelName || defaultModel || ''}
                                models={availableModels}
                                providers={availableProviders}
                                defaultModel={defaultModel}
                                status={modelsStatus}
                                onChange={(model) => {
                                    setPendingModelName(model);
                                    const cap = getMaxTokensForModel(model);
                                    setPendingParams((p) => ({ ...p, max_tokens: cap }));
                                }}
                            />
                            <span className="agent-field-hint">
                                {modelsStatus === MODEL_STATUS.LOADING
                                    ? 'Loading available models…'
                                    : modelsStatus === MODEL_STATUS.ERROR
                                        ? `Using fallback model. ${modelsError}`
                                        : 'URL and API key are configured in backend environment variables.'}
                            </span>
                        </div>
                    </div>

                    <div className="agent-config-section">
                        <h2 className="agent-config-section-title">Tools &amp; Skills</h2>
                        <CatalogPicker kind="tools"  attached={pendingTools}  onChange={setPendingTools} />
                        <CatalogPicker kind="skills" attached={pendingSkills} onChange={setPendingSkills} />
                    </div>

                    <div className="agent-config-section">
                        <h2 className="agent-config-section-title">Knowledge</h2>
                        <p style={{ fontSize: 11, color: '#6b7280', marginTop: -4, marginBottom: 10 }}>
                            Attach a knowledge corpus the agent can search at runtime.
                        </p>
                        <KnowledgeSection
                            value={pendingKnowledge}
                            onChange={setPendingKnowledge}
                            userDept={currentUser.department}
                            isApprover={currentUser.canApprove}
                            isAdmin={currentUser.role === 'admin'}
                        />
                    </div>

                    <div className="agent-config-section">
                        <h2 className="agent-config-section-title">Human-in-the-loop</h2>
                        <div className="agent-field">
                            <label className="agent-field-label">
                                Approval mode
                                <span className="agent-field-hint">When should a human review the agent's actions?</span>
                            </label>
                            <select
                                className="agent-field-select"
                                value={pendingHitl}
                                onChange={(e) => setPendingHitl(e.target.value)}
                            >
                                {HITL_MODES.map((m) => (
                                    <option key={m.value} value={m.value}>{m.label}</option>
                                ))}
                            </select>
                        </div>
                    </div>

                    <div className="agent-config-section">
                        <h2 className="agent-config-section-title">Parameters</h2>

                        <div className="agent-field">
                            <label className="agent-field-label">
                                Temperature
                                <span className="agent-field-hint">{Number(pendingParams.temperature).toFixed(2)}</span>
                            </label>
                            <input
                                type="range"
                                className="agent-field-range"
                                min="0" max="1" step="0.01"
                                value={pendingParams.temperature}
                                onChange={(e) => setPendingParams((p) => ({ ...p, temperature: parseFloat(e.target.value) }))}
                            />
                            <div className="agent-range-labels">
                                <span>Precise</span>
                                <span>Creative</span>
                            </div>
                        </div>

                        <div className="agent-field-row">
                            <div className="agent-field">
                                <label className="agent-field-label">
                                    Max Tokens
                                    <span className="agent-field-hint">1 – {getMaxTokensForModel(pendingModelName || defaultModel)}</span>
                                </label>
                                <input
                                    type="number"
                                    className="agent-field-input"
                                    min="1"
                                    max={getMaxTokensForModel(pendingModelName || defaultModel)}
                                    value={pendingParams.max_tokens}
                                    onChange={(e) => setPendingParams((p) => ({ ...p, max_tokens: parseInt(e.target.value || '0', 10) }))}
                                />
                            </div>
                            <div className="agent-field">
                                <label className="agent-field-label">
                                    Top P
                                    <span className="agent-field-hint">0 – 1</span>
                                </label>
                                <input
                                    type="number"
                                    className="agent-field-input"
                                    min="0" max="1" step="0.01"
                                    value={pendingParams.top_p}
                                    onChange={(e) => setPendingParams((p) => ({ ...p, top_p: parseFloat(e.target.value) }))}
                                />
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );

    // Existing-match cards (rendered below the message list when the backend
    // suggests a duplicate agent / template).
    const matchCards = stage === 'suggest_existing' && existingMatches.length > 0 ? (
        <div style={S.matchGroup}>
            {existingMatches.map((m) => {
                const conf = Math.round((m._match?.confidence || 0) * 100);
                const isTemplate = m.kind === 'agent_template';
                return (
                    <div key={`${m.kind}-${m.id}`} style={S.matchCard}>
                        <div style={S.matchCardTop}>
                            <span style={S.matchName}>{m.name}</span>
                            <span style={S.matchBadge}>{isTemplate ? 'Template' : 'Agent'} · {conf}% match</span>
                        </div>
                        {m.description && <div style={S.matchDesc}>{m.description}</div>}
                        {m._match?.reason && <div style={S.matchReason}>{m._match.reason}</div>}
                        <button
                            type="button"
                            style={S.matchOpenBtn(openingMatchId === m.id)}
                            onClick={() => handleOpenExisting(m)}
                            disabled={!!openingMatchId}
                        >
                            {openingMatchId === m.id ? (<><div style={S.btnSpinner} />Opening…</>) : (<>{isTemplate ? 'Use this template' : 'Open agent'}</>)}
                        </button>
                    </div>
                );
            })}
            {matchError && <div style={{ color: '#f87171', fontSize: '12px' }}>{matchError}</div>}
            <button type="button" style={S.buildAnywayBtn} onClick={handleBuildAnyway} disabled={isLoading || !!openingMatchId}>
                Continue building
            </button>
        </div>
    ) : null;

    const planCardNode = planCard && !isLoading ? (
        <PlanCard
            planCard={planCard}
            disabled={isLoading}
            onAccept={(answers) => { setPlanCard(null); handleSend(`__plan_card__:${JSON.stringify(answers)}`); }}
            onChangeSomething={() => { setPlanCard(null); handleSend("I'd like to change something — let's talk it through."); }}
        />
    ) : null;

    const hero = (
        <div style={S.heroCard}>
            <div style={S.heroIcon}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 2L13.5 10.5L22 12L13.5 13.5L12 22L10.5 13.5L2 12L10.5 10.5Z" />
                </svg>
            </div>
            <div style={S.heroTitle}>What should your agent do?</div>
            <div style={S.heroSub}>
                Describe what you need in plain language — I'll design the agent and fill the panel on the right. You can edit any field manually or ask me to tweak it. Click <strong>Deploy Agent</strong> when you're happy.
            </div>
        </div>
    );

    // Suggestions chips are hidden in the confirm stage — targeted edits are
    // driven purely by free-text there.
    const shownSuggestions = isConfirm ? [] : suggestions;

    // Right pane only appears once we have an assembled agent (confirm) or
    // we're in the post-deploy trigger flow. Before that the chat takes the
    // whole modal — same compact size as the Workflow / Skill factories.
    const showRightPane = isConfirm || isScheduling;

    return createPortal(
        // The trigger portal container has ``data-ac`` but no ancestor
        // ``data-ac`` — so the ABStudio CSS token block (defined on
        // ``:root, [data-ac]`` and rewritten by postcss-prefix-selector into
        // ``[data-ac] [data-ac]``) never applies to it. Every ``var(--…)``
        // reference (border, surface, text colours) resolves to nothing and
        // the input fields render as invisible boxes on white. Nesting a
        // second ``data-ac`` here makes the selector match and every token
        // — borders, surfaces, accent — light up correctly.
        <div data-ac="" style={{ display: 'contents' }}>
        <AnimatePresence>
            <fm.div
                key="factory-chat-overlay"
                className="factory-chat-overlay"
                style={base.overlay}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: motionTokens.base, ease: motionTokens.ease }}
            >
                <fm.div
                    ref={panelRef}
                    className="factory-chat-panel"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby={titleId}
                    tabIndex={-1}
                    style={{
                        ...base.panel,
                        // Widen only once the right pane appears; before that
                        // keep the standard factory modal footprint so the
                        // pre-confirm chat feels focused, not empty.
                        width: showRightPane ? 'min(1240px, 96vw)' : 'min(720px, 94vw)',
                        height: showRightPane ? 'min(90vh, 900px)' : 'min(88vh, 820px)',
                        maxHeight: showRightPane ? '90vh' : 'min(88vh, 820px)',
                        display: 'flex',
                        flexDirection: 'column',
                        transition: 'width 260ms cubic-bezier(0.2, 0.9, 0.35, 1), height 260ms cubic-bezier(0.2, 0.9, 0.35, 1)',
                    }}
                    onClick={(e) => e.stopPropagation()}
                    onKeyDown={onKeyDown}
                    initial={{ opacity: 0, y: 12, scale: 0.985 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: 8, scale: 0.985 }}
                    transition={{ duration: motionTokens.slow, ease: motionTokens.ease }}
                >
                    <div style={base.header}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', minWidth: 0 }}>
                            <div style={base.iconBadge}>{SPARK_ICON}</div>
                            <div style={{ minWidth: 0 }}>
                                <div id={titleId} style={{ fontWeight: 650, fontSize: '14px', color: 'var(--color-text-primary, #0f172a)' }}>
                                    Agent Factory
                                </div>
                                <div style={{ fontSize: '11px', color: 'var(--color-text-secondary, #64748b)', marginTop: '1px' }}>
                                    {showRightPane
                                        ? "Tweak anything on the right until you're happy — or ask me to change a field."
                                        : "Describe it, we'll build it."}
                                </div>
                            </div>
                        </div>
                        <button style={base.closeBtn} onClick={onClose} aria-label="Close">
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                        </button>
                    </div>

                    <div style={S.twoColBody}>
                        {/* ── Left: chat pane ── */}
                        <div style={showRightPane ? S.chatPaneSplit : S.chatPaneFull}>
                            <div style={base.messagesArea}>
                                {messages.length === 0 && hero}
                                {messages.map((msg) => (
                                    <MessageRow
                                        key={msg.id}
                                        msg={msg}
                                        onDownload={handleDownloadGenerated}
                                    />
                                ))}
                                {planCardNode || matchCards}
                                <div ref={messagesEndRef} />
                            </div>

                            {shownSuggestions.length > 0 && !isLoading && (
                                <div style={{ padding: '6px 16px 12px', flexShrink: 0 }}>
                                    <AnswerCards
                                        suggestions={shownSuggestions}
                                        onSelect={handleChipClick}
                                        disabled={isLoading}
                                    />
                                </div>
                            )}

                            {!isScheduling && (
                                <div style={base.inputArea}>
                                    <textarea
                                        ref={inputRef}
                                        style={base.textarea}
                                        placeholder={
                                            isConfirm
                                                ? 'Ask me to tweak a field (e.g. "make the system prompt more aggressive")…'
                                                : 'Describe what your agent should do…'
                                        }
                                        value={inputValue}
                                        onChange={(e) => setInputValue(e.target.value)}
                                        onInput={(e) => autoGrowTextarea(e.target)}
                                        onKeyDown={handleTextareaKey}
                                        rows={1}
                                        disabled={isLoading}
                                        aria-label="Message"
                                    />
                                    <button
                                        style={base.sendBtn(!inputValue.trim() || isLoading)}
                                        onClick={handleSendClick}
                                        disabled={!inputValue.trim() || isLoading}
                                        aria-label="Send"
                                    >
                                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                            <line x1="22" y1="2" x2="11" y2="13" />
                                            <polygon points="22 2 15 22 11 13 2 9 22 2" />
                                        </svg>
                                    </button>
                                </div>
                            )}
                        </div>

                        {/* ── Right: config pane (only after confirm) ── */}
                        {showRightPane && (
                            <div style={S.rightPaneWrap}>{rightPane}</div>
                        )}
                    </div>
                </fm.div>
            </fm.div>
            {downloadNotice && <DownloadNotice notice={downloadNotice} />}
        </AnimatePresence>
        </div>,
        portalContainer,
    );
}

function MessageRow({ msg, onDownload }) {
    if (msg.type === 'steps') return <StepsBlock block={msg} />;
    const isUser = msg.role === 'user';
    return (
        <div className="factory-chat-msg-enter" style={isUser ? base.userRow : base.aiRow}>
            <div style={isUser ? base.userBubble : base.aiBubble(msg.isError)}>
                {isUser ? (
                    <span style={{ whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{msg.content}</span>
                ) : (
                    <div style={{ lineHeight: 1.5 }} className="agent-chat-md">
                        <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>
                            {stripEmoji(msg.content)}
                        </ReactMarkdown>
                        <FactoryFileChips files={sniffGeneratedFiles(msg.content)} onDownload={onDownload} />
                    </div>
                )}
            </div>
        </div>
    );
}

function StepsBlock({ block }) {
    if (block.steps.length === 0 && !block.done) {
        return (
            <div style={S.stepsBlock} aria-live="polite">
                <div style={S.stepRow}>
                    <div style={S.stepSpinner} />
                    <span style={S.stepTextActive}>Thinking…</span>
                </div>
            </div>
        );
    }
    return (
        <div style={S.stepsBlock} aria-live="polite">
            {block.steps.map((step, i) => (
                <div key={i} style={S.stepRow}>
                    {step.status === 'active' ? (
                        <div style={S.stepSpinner} />
                    ) : (
                        <div style={S.stepCheck}>
                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none"
                                 stroke="#16a34a" strokeWidth="3" strokeLinecap="round">
                                <polyline points="20 6 9 17 4 12" />
                            </svg>
                        </div>
                    )}
                    <span style={step.status === 'active' ? S.stepTextActive : S.stepTextDone}>
                        {step.text}
                    </span>
                </div>
            ))}
        </div>
    );
}

const S = {
    twoColBody: {
        flex: '1 1 auto', minHeight: 0, display: 'flex', flexDirection: 'row',
        alignItems: 'stretch', overflow: 'hidden',
    },
    chatPaneSplit: {
        flex: '0 0 46%', minWidth: 0, display: 'flex', flexDirection: 'column',
        overflow: 'hidden', borderRight: '1px solid var(--color-border-subtle, #eef2f7)',
    },
    chatPaneFull: {
        flex: '1 1 auto', minWidth: 0, display: 'flex', flexDirection: 'column',
        overflow: 'hidden',
    },
    rightPaneWrap: {
        flex: '1 1 auto', minWidth: 0, display: 'flex', flexDirection: 'column',
        overflow: 'hidden', background: 'var(--color-surface, #ffffff)',
    },
    configPane: {
        display: 'flex', flexDirection: 'column', flex: '1 1 auto', minHeight: 0, overflow: 'hidden',
    },
    configHeader: {
        display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
        gap: '12px', padding: '14px 18px',
        borderBottom: '1px solid var(--color-border-subtle, #eef2f7)',
        background: '#fbfcfd', flexShrink: 0,
    },
    configHeaderTitle: { fontSize: '13px', fontWeight: 700, color: '#0f172a', letterSpacing: '0.01em' },
    configHeaderSub: { marginTop: '2px', fontSize: '11.5px', color: '#64748b', maxWidth: '440px', lineHeight: 1.45 },
    configScroll: {
        flex: '1 1 auto', minHeight: 0, overflowY: 'auto', padding: '16px 20px 24px',
    },
    configError: {
        margin: '10px 18px 0', padding: '8px 12px', background: 'rgba(220,38,38,0.06)',
        border: '1px solid rgba(220,38,38,0.25)', color: '#b91c1c', fontSize: '12px', borderRadius: '8px',
    },
    deployBtn: (disabled) => ({
        display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '9px 16px',
        background: disabled ? '#c7d2fe' : '#4f46e5', border: 'none', borderRadius: '10px',
        color: '#fff', fontSize: '13px', fontWeight: 600, cursor: disabled ? 'not-allowed' : 'pointer',
        flexShrink: 0, transition: 'all 0.15s',
        boxShadow: disabled ? 'none' : '0 4px 12px rgba(99,102,241,0.35)',
    }),
    btnSpinner: {
        width: '12px', height: '12px', borderRadius: '50%',
        border: '2px solid rgba(255,255,255,0.3)', borderTopColor: '#fff',
        animation: 'spin 0.8s linear infinite', marginRight: '6px',
    },
    heroCard: {
        display: 'flex', flexDirection: 'column', alignItems: 'center', textAlign: 'center',
        padding: '28px 20px 20px', margin: 'auto 0',
    },
    heroIcon: {
        width: '44px', height: '44px', borderRadius: '14px',
        background: 'linear-gradient(135deg, #eef2ff, #e0e7ff)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: '#4f46e5', marginBottom: '14px', boxShadow: '0 2px 8px rgba(99,102,241,0.12)',
    },
    heroTitle: { fontSize: '15px', fontWeight: 700, color: '#0f172a', letterSpacing: '-0.01em', marginBottom: '6px' },
    heroSub: { fontSize: '12.5px', color: '#64748b', lineHeight: 1.6, maxWidth: '380px' },
    matchGroup: { display: 'flex', flexDirection: 'column', gap: '10px', padding: '4px 0 2px' },
    matchCard: {
        display: 'flex', flexDirection: 'column', gap: '6px', padding: '12px 14px',
        background: '#ffffff', border: '1px solid #dbe2ea', borderRadius: '12px',
        boxShadow: '0 1px 3px rgba(15,23,42,0.05)',
    },
    matchCardTop: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' },
    matchName: {
        fontSize: '13px', fontWeight: 650, color: '#0f172a',
        minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
    },
    matchBadge: {
        flexShrink: 0, fontSize: '10.5px', fontWeight: 600, color: '#4f46e5',
        background: '#eef2ff', borderRadius: '999px', padding: '2px 8px',
    },
    matchDesc: { fontSize: '12px', color: '#475569', lineHeight: 1.5 },
    matchReason: { fontSize: '11.5px', color: '#64748b', fontStyle: 'italic', lineHeight: 1.45 },
    matchOpenBtn: (busy) => ({
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: '6px',
        alignSelf: 'flex-start', marginTop: '2px', padding: '7px 16px',
        background: busy ? '#c7d2fe' : '#4f46e5', border: 'none', borderRadius: '9px',
        color: '#fff', fontSize: '12.5px', fontWeight: 600,
        cursor: busy ? 'default' : 'pointer',
        boxShadow: busy ? 'none' : '0 3px 10px rgba(99,102,241,0.28)',
    }),
    buildAnywayBtn: {
        alignSelf: 'flex-start', padding: '7px 14px', background: 'transparent',
        border: '1px dashed #cbd5e1', borderRadius: '9px', color: '#475569',
        fontSize: '12px', fontWeight: 550, cursor: 'pointer',
    },
    triggerPanel: {
        display: 'flex', flexDirection: 'column', flex: '1 1 auto', minHeight: 0,
        borderTop: '1px solid rgba(15,23,42,0.06)',
        background: 'linear-gradient(180deg, #f8fafc, #ffffff)',
    },
    triggerSuccess: {
        display: 'flex', alignItems: 'flex-start', gap: '10px', padding: '12px 18px',
        background: 'rgba(22,163,74,0.06)', borderBottom: '1px solid rgba(22,163,74,0.15)', flexShrink: 0,
    },
    triggerSuccessIcon: {
        width: '26px', height: '26px', borderRadius: '50%', background: 'rgba(22,163,74,0.18)',
        color: '#16a34a', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
    },
    triggerSuccessTitle: { fontSize: '13px', fontWeight: 600, color: '#0f172a' },
    triggerSuccessSub: { marginTop: '2px', fontSize: '11.5px', color: '#64748b', lineHeight: 1.5 },
    triggerScroll: { flex: '1 1 auto', minHeight: 0, overflowY: 'auto', padding: '14px 18px 6px' },
    triggerActions: {
        display: 'flex', justifyContent: 'flex-end', padding: '12px 18px',
        borderTop: '1px solid rgba(15,23,42,0.06)', background: '#ffffff', flexShrink: 0,
    },
    triggerFinishBtn: {
        display: 'inline-flex', alignItems: 'center', padding: '9px 18px',
        background: 'linear-gradient(135deg, #4f46e5, #7c3aed)', border: 'none', borderRadius: '10px',
        color: '#fff', fontSize: '13px', fontWeight: 600, cursor: 'pointer',
        boxShadow: '0 4px 12px rgba(99,102,241,0.35)',
    },
    // Steps block copied from FactoryChatShell so the visual is identical.
    stepsBlock: {
        display: 'flex', flexDirection: 'column', gap: '4px', padding: '10px 14px',
        background: 'linear-gradient(135deg, #f8fafc, #f1f5f9)',
        border: '1px solid var(--color-border-subtle, #e2e8f0)',
        borderRadius: 'var(--radius-md, 12px)',
    },
    stepRow: { display: 'flex', alignItems: 'center', gap: '10px', minHeight: '26px' },
    stepSpinner: {
        width: '14px', height: '14px', borderRadius: '50%', flexShrink: 0,
        border: '2px solid rgba(148,163,184,0.2)',
        borderTopColor: 'var(--color-accent, #4f46e5)',
        animation: 'spin 0.7s linear infinite',
    },
    stepCheck: {
        width: '14px', height: '14px', borderRadius: '50%', flexShrink: 0,
        background: 'rgba(22,163,74,0.1)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
    },
    stepTextActive: { fontSize: '12.5px', color: 'var(--color-text-primary, #1e293b)', fontWeight: 550 },
    stepTextDone: { fontSize: '12.5px', color: 'var(--color-text-muted, #94a3b8)', fontWeight: 400 },
};

export default AgentFactoryChat;
