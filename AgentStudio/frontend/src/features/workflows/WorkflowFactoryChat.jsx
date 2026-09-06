// SPDX-License-Identifier: MIT
import { useState, useEffect, useCallback, useRef } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion as fm } from 'framer-motion';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { API_BASE, buildAuthHeaders } from '../../config/api';
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
import WorkflowPreview from './WorkflowPreview';

const STAR_ICON = (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
        <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" />
    </svg>
);

const REMARK_PLUGINS = [remarkGfm];
const FOCUSABLE =
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Backend writes tools/skills as strings pre-injection or {name} objects
// post-injection. The preview components expect the object form.
const _normaliseAttached = (raw) =>
    (raw || [])
        .map((x) => (typeof x === 'string' ? { name: x } : x))
        .filter((x) => x && x.name);

const _normaliseNodes = (nodes) => (nodes || []).map((n) => {
    if (n?.type !== 'agent') return n;
    const d = n.data || {};
    return {
        ...n,
        data: {
            ...d,
            tools: _normaliseAttached(d.tools),
            skills: _normaliseAttached(d.skills),
        },
    };
});

// ---------------------------------------------------------------------------
// WorkflowFactoryChat — two-pane "Create with AI" modal for workflows.
//
// Mirrors the finished AgentFactoryChat: full-width chat until a workflow is
// generated, then splits into chat-on-left + live-preview-on-right. The
// preview is a real React Flow canvas driven by local state — the user can
// drag, click, edit fields, add nodes, wire handles. The chat drives targeted
// patches (WorkflowFieldPatcher on the backend). Nothing persists until the
// user hits Deploy in the preview header.
// ---------------------------------------------------------------------------
function WorkflowFactoryChat({ onClose, onCreated }) {
    const portalContainer = useTriggerPortalContainer();
    const panelRef = useRef(null);
    const inputRef = useRef(null);
    const messagesEndRef = useRef(null);
    const previouslyFocused = useRef(null);
    const titleId = useRef(`factory-chat-title-${Math.floor(Math.random() * 1e6)}`).current;

    // ── Live workflow state (right pane owns the source of truth) ──
    const [workflowName, setWorkflowName] = useState('');
    const [nodes, setNodes] = useState([]);
    const [edges, setEdges] = useState([]);

    // ── Deploy state ──
    const [deployedWorkflow, setDeployedWorkflow] = useState(null);
    const [isDeploying, setIsDeploying] = useState(false);
    const [deployError, setDeployError] = useState('');
    const [scheduled, setScheduled] = useState(false);

    // ── Chat / SSE plumbing ──
    const [inputValue, setInputValue] = useState('');
    const [existingMatches, setExistingMatches] = useState([]);
    const [openingMatchId, setOpeningMatchId] = useState(null);
    const [matchError, setMatchError] = useState('');
    const [planCard, setPlanCard] = useState(null);
    const [lastPlanAnswers, setLastPlanAnswers] = useState(null);
    const [serviceWarning, setServiceWarning] = useState(null);

    // Live snapshot sent as ``current_workflow_override`` on each chat turn
    // so the backend patcher operates on the user's actual view, not the
    // last blueprint. Held in a ref to avoid churn on every keystroke.
    const currentWorkflowRef = useRef({});
    useEffect(() => {
        currentWorkflowRef.current = {
            name: workflowName,
            graph_data: { nodes, edges },
        };
    }, [workflowName, nodes, edges]);

    const buildExtraBody = useCallback(() => {
        if (nodes.length === 0) return {};
        return { current_workflow_override: currentWorkflowRef.current };
    }, [nodes.length]);

    // Merge backend-returned workflow into local state. Preserve any node
    // positions the user has already dragged so a follow-up patch doesn't
    // snap everything back to Dagre defaults.
    const mergeWorkflow = useCallback((incoming) => {
        if (!incoming) return;
        const nextName = incoming.name || workflowName || 'Workflow';
        const incomingNodes = _normaliseNodes(incoming.graph_data?.nodes || []);
        const incomingEdges = incoming.graph_data?.edges || [];

        setWorkflowName(nextName);
        setNodes((prev) => {
            const prevById = new Map(prev.map((n) => [n.id, n]));
            return incomingNodes.map((n) => {
                const carry = prevById.get(n.id);
                if (carry && carry.position && !n.position) return { ...n, position: carry.position };
                if (carry && carry.position) {
                    // Prefer the user's manual position over the backend's default.
                    return { ...n, position: carry.position };
                }
                return n;
            });
        });
        setEdges(incomingEdges);
    }, [workflowName]);

    const onMessage = useCallback((ev) => {
        if (ev.data?.workflow) {
            mergeWorkflow(ev.data.workflow);
        }
        if (ev.stage === 'plan_card') {
            setPlanCard(ev.data?.plan_card ?? null);
            setServiceWarning(null);
        } else if (ev.stage === 'plan_card_service_warning') {
            setPlanCard(null);
            setServiceWarning({ services: ev.data?.unsatisfiable_services || [] });
        } else if (ev.stage) {
            setPlanCard(null);
            setServiceWarning(null);
        }
        if (ev.stage === 'suggest_existing') {
            setExistingMatches(ev.data?.existing_matches || []);
        } else if (ev.data?.existing_matches === undefined) {
            setExistingMatches([]);
        }
    }, [mergeWorkflow]);

    const onReset = useCallback(() => {
        setDeployError('');
        setExistingMatches([]);
        setMatchError('');
        setPlanCard(null);
        setServiceWarning(null);
    }, []);

    const {
        messages, suggestions, stage, isLoading, sessionId, sendMessage,
    } = useFactoryChatStream({
        endpoint: '/workflow-factory/chat',
        onMessage,
        onReset,
        buildExtraBody,
    });

    const [downloadNotice, setDownloadNotice] = useTransientNotice();
    const handleDownloadGenerated = useCallback(async (file) => {
        const result = await downloadGeneratedFile(absoluteDownloadUrl(file), file.filename);
        if (result.status !== 'ok') {
            setDownloadNotice({ kind: result.status, text: result.message });
        }
    }, [setDownloadNotice]);

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
            const res = await fetch(`${API_BASE}/templates/${match.id}/use`, {
                method: 'POST',
                headers: buildAuthHeaders(),
            });
            const wf = await res.json();
            if (!res.ok) throw new Error(wf.detail || 'Could not use template');
            onCreated(wf);
        } catch (err) {
            setMatchError(err.message);
            setOpeningMatchId(null);
        }
    };

    const handleBuildAnyway = () => {
        if (isLoading) return;
        handleSend("None of these fit — let's continue building a new workflow.");
    };

    const handleDeploy = useCallback(async () => {
        if (!sessionId || isDeploying || nodes.length === 0) return;
        setIsDeploying(true);
        setDeployError('');
        try {
            const res = await fetch(`${API_BASE}/workflow-factory/confirm`, {
                method: 'POST',
                headers: buildAuthHeaders(),
                body: JSON.stringify({
                    session_id: sessionId,
                    name_override: workflowName || undefined,
                    graph_data_override: { nodes, edges },
                }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Deployment failed');

            const wfRes = await fetch(`${API_BASE}/workflows`, {
                method: 'POST',
                headers: buildAuthHeaders(),
                body: JSON.stringify({ name: data.name, graphData: data.graph_data }),
            });
            const wf = await wfRes.json();
            if (!wfRes.ok) throw new Error(wf.detail || 'Could not save the workflow');

            setDeployedWorkflow(wf);
            setScheduled(true);
        } catch (err) {
            setDeployError(err.message);
        } finally {
            setIsDeploying(false);
        }
    }, [sessionId, isDeploying, workflowName, nodes, edges]);

    const handleFinishScheduled = () => {
        if (deployedWorkflow) onCreated(deployedWorkflow);
        else onClose();
    };

    // ── Focus trap + Escape + focus restore ──
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
        const focusables = Array.from(panel.querySelectorAll(FOCUSABLE))
            .filter((n) => n.offsetParent !== null || n === document.activeElement);
        if (focusables.length === 0) return;
        const first = focusables[0]; const last = focusables[focusables.length - 1];
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

    const isConfirm = stage === 'confirm' && nodes.length > 0;
    const isScheduling = scheduled;
    const showRightPane = isConfirm || isScheduling;

    // Existing-match cards.
    const matchCards = stage === 'suggest_existing' && existingMatches.length > 0 ? (
        <div style={S.matchGroup}>
            {existingMatches.map((m) => {
                const conf = Math.round((m._match?.confidence || 0) * 100);
                const isTemplate = m.kind === 'workflow_template';
                return (
                    <div key={`${m.kind}-${m.id}`} style={S.matchCard}>
                        <div style={S.matchCardTop}>
                            <span style={S.matchName}>{m.name}</span>
                            <span style={S.matchBadge}>{isTemplate ? 'Template' : 'Workflow'} · {conf}% match</span>
                        </div>
                        {m.description && <div style={S.matchDesc}>{m.description}</div>}
                        {m._match?.reason && <div style={S.matchReason}>{m._match.reason}</div>}
                        <button
                            type="button"
                            style={S.matchOpenBtn(openingMatchId === m.id)}
                            onClick={() => handleOpenExisting(m)}
                            disabled={!!openingMatchId}
                        >
                            {openingMatchId === m.id ? (<><span style={S.spinner} />Opening…</>) : (<>{isTemplate ? 'Use & open' : 'Open workflow'}</>)}
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
            onAccept={(answers) => {
                setPlanCard(null);
                setLastPlanAnswers(answers);
                handleSend(`__plan_card__:${JSON.stringify(answers)}`);
            }}
            onChangeSomething={() => { setPlanCard(null); handleSend("I'd like to change something — let's talk it through."); }}
        />
    ) : null;

    const serviceWarningNode = serviceWarning && !isLoading ? (
        <div style={WARN.box}>
            <div style={WARN.title}>
                ⚠️ {(serviceWarning.services || []).join(', ')} {serviceWarning.services?.length === 1 ? 'has' : 'have'} no tools in the catalog
            </div>
            <div style={WARN.body}>
                Generation will proceed but no tools will be attached for {serviceWarning.services?.length === 1 ? 'it' : 'them'}.
            </div>
            <div style={WARN.actions}>
                <button
                    type="button"
                    style={WARN.continueBtn}
                    onClick={() => {
                        setServiceWarning(null);
                        const ack = { ...(lastPlanAnswers || {}), _svc_warning_ack: true };
                        handleSend(`__plan_card__:${JSON.stringify(ack)}`);
                    }}
                >
                    Continue anyway
                </button>
                <button
                    type="button"
                    style={WARN.backBtn}
                    onClick={() => { setServiceWarning(null); handleSend("Let me adjust the external systems."); }}
                >
                    Go back
                </button>
            </div>
        </div>
    ) : null;

    const hero = (
        <div style={S.heroCard}>
            <div style={S.heroIcon}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                    <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" />
                </svg>
            </div>
            <div style={S.heroTitle}>What would you like to automate?</div>
            <div style={S.heroSub}>
                Describe your workflow in plain language — I'll design the pipeline and fill the preview on the right. You can drag nodes, edit any field, or ask me to tweak things. Click <strong>Deploy Workflow</strong> when you're happy.
            </div>
        </div>
    );

    const shownSuggestions = isConfirm ? [] : suggestions;

    // Right pane content — either deploy-success trigger panel or the preview.
    const rightPane = isScheduling && deployedWorkflow ? (
        <div style={S.triggerPanel}>
            <style>{TRIGGER_SCOPED_CSS}</style>
            <div style={S.triggerSuccess}>
                <div style={S.triggerSuccessIcon}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round">
                        <polyline points="20 6 9 17 4 12" />
                    </svg>
                </div>
                <div style={{ minWidth: 0 }}>
                    <div style={S.triggerSuccessTitle}>
                        Workflow saved — <strong>{deployedWorkflow.name}</strong>
                    </div>
                    <div style={S.triggerSuccessSub}>
                        Add a trigger to run it automatically, or click <em style={{ color: '#4f46e5', fontStyle: 'normal', fontWeight: 600 }}>Finish</em> to open the editor.
                    </div>
                </div>
            </div>
            <div style={S.triggerScroll}>
                <TriggerSection targetKind="workflow" targetId={deployedWorkflow.id} variant="card" />
            </div>
            <div style={S.triggerActions}>
                <button type="button" style={S.triggerFinishBtn} onClick={handleFinishScheduled}>
                    Finish
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" style={{ marginLeft: 6 }}>
                        <line x1="5" y1="12" x2="19" y2="12" /><polyline points="12 5 19 12 12 19" />
                    </svg>
                </button>
            </div>
        </div>
    ) : (
        <WorkflowPreview
            name={workflowName}
            nodes={nodes}
            edges={edges}
            onNameChange={setWorkflowName}
            onNodesChange={setNodes}
            onEdgesChange={setEdges}
            onDeploy={handleDeploy}
            isDeploying={isDeploying}
            deployError={deployError}
        />
    );

    return createPortal(
        // Second data-ac wrapper so [data-ac] [data-ac] selectors in the
        // light-theme token block resolve inside the portal. Same trick the
        // AgentFactoryChat uses to keep borders / surfaces visible.
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
                        width: showRightPane ? 'min(1360px, 96vw)' : 'min(720px, 94vw)',
                        height: showRightPane ? 'min(92vh, 940px)' : 'min(88vh, 820px)',
                        maxHeight: showRightPane ? '92vh' : 'min(88vh, 820px)',
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
                            <div style={base.iconBadge}>{STAR_ICON}</div>
                            <div style={{ minWidth: 0 }}>
                                <div id={titleId} style={{ fontWeight: 650, fontSize: '14px', color: 'var(--color-text-primary, #0f172a)' }}>
                                    Workflow Factory
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
                                {planCardNode || serviceWarningNode || matchCards}
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
                                                ? 'Ask me to tweak a field or node…'
                                                : 'Describe what your workflow should do…'
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
        flex: '0 0 40%', minWidth: 0, display: 'flex', flexDirection: 'column',
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
    spinner: {
        width: '12px', height: '12px', borderRadius: '50%',
        border: '2px solid rgba(255,255,255,0.3)', borderTopColor: '#fff',
        animation: 'spin 0.8s linear infinite', marginRight: '6px', display: 'inline-block',
    },
    // Steps block styling copied from FactoryChatShell.
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
};

const WARN = {
    box: {
        display: 'flex', flexDirection: 'column', gap: '8px', padding: '13px 16px',
        margin: '4px 0 2px', background: '#fffbeb', border: '1px solid #fde68a',
        borderRadius: '12px',
    },
    title: { fontSize: '13px', fontWeight: 700, color: '#92400e' },
    body: { fontSize: '12px', color: '#78716c', lineHeight: 1.5 },
    actions: { display: 'flex', gap: '8px', marginTop: '2px' },
    continueBtn: {
        padding: '8px 16px', borderRadius: '9px', border: 'none',
        background: '#4f46e5', color: '#fff', fontSize: '12.5px', fontWeight: 600, cursor: 'pointer',
    },
    backBtn: {
        padding: '8px 14px', borderRadius: '9px', border: '1px solid #e2e8f0',
        background: '#fff', color: '#475569', fontSize: '12.5px', fontWeight: 550, cursor: 'pointer',
    },
};

export default WorkflowFactoryChat;
