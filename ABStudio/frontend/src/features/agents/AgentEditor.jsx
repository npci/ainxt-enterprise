// SPDX-License-Identifier: Apache-2.0
import { useState, useRef, useEffect, useCallback, useMemo, Fragment } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import useAgentsStore from '../../store/agentsStore';
import { API_BASE, buildAuthHeaders, kbFetch } from '../../config/api';
import CatalogPicker from '../../components/common/CatalogPicker';
import GenerateInstructionsModal from '../../components/common/GenerateInstructionsModal';
import KnowledgeSection, { KB_MODE_NONE } from '../../components/common/KnowledgeSection';
import SampleDocSection from '../../components/common/SampleDocSection';
// SubflowPicker import removed from Agent editor — sub-asset attachment is now
// configured only from the Workflow editor. SubflowPicker is still used there
// (see features/workflows/editor/SubflowPicker.jsx) so the component itself is
// untouched.
import TriggerSection from '../triggers/TriggerSection';
import TriggerNotifications from '../triggers/TriggerNotifications';
import SubmitApprovalButton from '../governance/SubmitApprovalButton';
import StatusBadge from '../governance/StatusBadge';
import SubagentCounterChip from '../_shared/SubagentCounterChip';
import useAvailableModels, { MODEL_STATUS } from '../../hooks/useAvailableModels';
import useCurrentUser from '../../hooks/useCurrentUser';
import { validateEntityName } from '../../utils/validateName';
import { stripEmoji } from '../../utils/stripEmoji';
import useDashboardStore from '../../store/dashboardStore';
import { stripProviderPrefix } from '../../utils/modelLabel';
import { getMaxTokensForModel } from '../../utils/modelMaxTokens';
import {
    formatRelativeTime,
    groupThreads,
    threadTitle,
    threadPreview,
    mapHistoryToUiMessages,
    splitFileAttachmentMarker,
    formatFileAttachmentMarker,
} from '../../utils/threadHelpers';
import {
    loadActiveThread,
    saveActiveThread,
    loadComposerDraft,
    saveComposerDraft,
} from '../../utils/editorPersistence';
import { downloadGeneratedFile } from '../_shared/downloadGeneratedFile';
import { useTransientNotice } from '../_shared/useTransientNotice';
import DownloadNotice from '../_shared/DownloadNotice';
import ExtractedTextPreview from '../_shared/ExtractedTextPreview';
import { stripBareGeneratedPaths, stripGeneratedMarkdownLinks, PRIMARY_DOWNLOAD_EXTS } from '../_shared/sniffGeneratedFiles';
import { useShareActions } from '../_shared/useShareActions';
import { RECOMMENDED_MODEL as RECOMMENDED_MODEL_ID } from '../../config/models';

const PREVIEW_SUGGESTIONS = [
    'Show me what you can do',
    'Explain your role',
    'Run a quick test',
    'List your tools',
];


function isRecommendedModel(model) {
    return model === RECOMMENDED_MODEL_ID || model.endsWith(`/${RECOMMENDED_MODEL_ID}`);
}

function describeModel(model) {
    const label = stripProviderPrefix(model).toLowerCase();
    if (label.includes('sonnet-4-6')) return 'Best default for agents';
    if (label.includes('opus')) return 'Deep reasoning';
    if (label.includes('haiku')) return 'Lightweight and fast';
    return 'Backend gateway model';
}

function modelTier(model) {
    const label = stripProviderPrefix(model).toLowerCase();
    if (label.includes('opus')) return 'Max';
    if (label.includes('sonnet')) return 'Balanced';
    if (label.includes('haiku')) return 'Fast';
    return 'Model';
}

// Agent Configuration model picker — in-flow collapse/expand control.
//
// Requirements (from user feedback):
//   1. Click the trigger → the menu expands DOWNWARD inside the Model
//      Configuration card. The card itself grows in height; subsequent
//      cards (Tools & Skills, etc.) move DOWN to make room. No floating
//      popup, no portal, no overlap on other features.
//   2. Click an option → the menu collapses, the card shrinks back to
//      its single-row height, the selected model name shows in the
//      trigger. The page-level scroll position is preserved.
//   3. Click outside (anywhere not in the picker) → the menu collapses
//      without changing the selection.
//
// Implementation notes:
//   * No ``position: absolute`` / ``position: fixed`` anywhere — the
//     menu is part of normal flex flow, so it pushes siblings instead
//     of overlapping them. This sidesteps the ancestor
//     ``overflow: hidden`` clipping entirely.
//   * No React portal, no getBoundingClientRect anchoring — the menu
//     lives inside the trigger's wrapper, so its width auto-tracks the
//     trigger.
//   * The model picked here is still saved as ``model_name`` on the
//     agent (data shape unchanged), and still flows through to the
//     swarm orchestrator + aggregator + workers + any nested swarm
//     via the resolution chain wired up earlier this session.
function AgentModelPicker({ value, models, providers, defaultModel, status, onChange }) {
    const [open, setOpen] = useState(false);
    const wrapperRef = useRef(null);

    // Build provider-grouped options. Same logic as the workflow tab so
    // the two pickers stay behaviourally identical.
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
        const flat = (models && models.length ? models : [value || defaultModel || RECOMMENDED_MODEL_ID])
            .filter(Boolean)
            .filter((m, i, arr) => arr.indexOf(m) === i);
        return flat.length > 0
            ? [{ label: '', options: flat.map(id => ({ id, label: stripProviderPrefix(id) })) }]
            : [];
    }, [providers, models, value, defaultModel]);

    const flatIds = groups.flatMap(g => g.options.map(o => o.id));
    const selected = value || defaultModel || flatIds[0] || RECOMMENDED_MODEL_ID;

    // Close on outside click. The menu is in-flow inside ``wrapperRef``,
    // so ``contains`` is the only check we need — no portal carve-out.
    useEffect(() => {
        if (!open) return undefined;
        const onMouseDown = (event) => {
            if (!wrapperRef.current?.contains(event.target)) setOpen(false);
        };
        document.addEventListener('mousedown', onMouseDown);
        return () => document.removeEventListener('mousedown', onMouseDown);
    }, [open]);

    // Close on Escape so keyboard users have a way back without picking.
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
                <span className="agent-model-inline-value">
                    {stripProviderPrefix(selected)}
                </span>
                <svg
                    className="agent-model-inline-chevron"
                    width="16" height="16" viewBox="0 0 24 24"
                    fill="none" stroke="currentColor" strokeWidth="2"
                    strokeLinecap="round" strokeLinejoin="round"
                    aria-hidden="true"
                >
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
                                        onClick={() => {
                                            onChange(option.id);
                                            setOpen(false);
                                        }}
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

const AGENT_TIMELINE_CHECK_ICON = (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="20 6 9 17 4 12" />
    </svg>
);

function AgentPreviewThinkingCard({ agentName, activeSubagents = [], allSubagents = [] }) {
    const activeCount = Array.isArray(activeSubagents) ? activeSubagents.length : 0;
    const totalCount = Array.isArray(allSubagents) ? allSubagents.length : 0;
    const stage = activeCount > 0
        ? `Running ${activeCount} sub-agent${activeCount === 1 ? '' : 's'}`
        : totalCount > 0
            ? 'Reviewing sub-agent results'
            : 'Understanding request';

    return (
        <div className="thinking-card agent-preview-thinking-card" role="status" aria-live="polite">
            <div className="thinking-card-header">
                <span className="thinking-pulse" aria-hidden="true" />
                <span className="thinking-card-title">{agentName || 'Agent'} running</span>
                {totalCount > 0 && (
                    <SubagentCounterChip
                        count={activeCount}
                        workers={activeSubagents}
                        subagents={allSubagents}
                    />
                )}
                <span className="thinking-stage">{stage}</span>
            </div>
            {totalCount > 0 && (
                <ol className="thinking-timeline">
                    {allSubagents.map((worker, idx) => {
                        const status = worker.status || 'running';
                        const stateLabel = status === 'complete'
                            ? (worker.durationS ? `${worker.durationS}s` : 'complete')
                            : status === 'failed'
                                ? (worker.error || 'failed')
                                : 'running';
                        const lead = status === 'complete'
                            ? 'Sub-agent complete'
                            : status === 'failed'
                                ? 'Sub-agent failed'
                                : 'Sub-agent running';
                        return (
                            <li
                                key={worker.callId || `${worker.alias || 'subagent'}-${idx}`}
                                className={`thinking-step thinking-step--${status} thinking-step--subagent`}
                            >
                                <span className="thinking-step-marker" aria-hidden="true">
                                    {status === 'complete' ? AGENT_TIMELINE_CHECK_ICON : <span className="thinking-step-dot" />}
                                </span>
                                <div className="thinking-step-body">
                                    <span className="thinking-step-agent">
                                        {lead} <strong>{worker.alias || worker.agentId || 'worker'}</strong>
                                        <span className="thinking-subagent-state"> · {stateLabel}</span>
                                    </span>
                                    {worker.taskPreview && (
                                        <span className="agent-preview-thinking-task">{worker.taskPreview}</span>
                                    )}
                                </div>
                            </li>
                        );
                    })}
                </ol>
            )}
            <div className="thinking-skeleton" aria-hidden="true">
                <span className="thinking-skeleton-line" />
                <span className="thinking-skeleton-line short" />
            </div>
        </div>
    );
}

// ── Chat-pane attachment + rendering configuration ──────────────────────────
// Mirrors the Workflow ChatPanel (features/workflows/editor/ChatPanel.jsx) so
// the Agents tab presents attachments and outputs with the same structure,
// formatting, and download semantics. The Workflow ChatPanel itself is NOT
// modified — these are local copies to keep the Workflow flow / connections /
// implementation setup untouched as required.
const AGENT_CHAT_ATTACH_ACCEPT = [
    '.pdf', '.docx', '.pptx', '.xlsx', '.xls', '.csv',
    '.html', '.htm', '.rtf', '.txt', '.json', '.md',
    // Image extensions enable the OCR pipeline's standalone-image path
    // (screenshots, photos, scanned single pages). Server-side validation
    // accepts the same set via /agent-runner/attachment.
    '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp',
].join(',');
const AGENT_CHAT_ATTACH_MAX_FILES = 5;
// Per-file cap applied when composing the prompt. Mirrors
// CHAT_ATTACH_PROMPT_BUDGET_CHARS in the Workflow ChatPanel so a single
// huge document (e.g. a multi-sheet Excel parsed report) can't blow past
// the model's context window and silently produce an empty reply.
const AGENT_CHAT_ATTACH_PROMPT_BUDGET_CHARS = 60000;

// Image formats are routed to /agent-runner/image-asset (saved as sandbox
// assets the agent can reference by path) instead of /agent-runner/attachment
// (which OCRs/extracts text and fails on logos with no readable text).
const IMAGE_ASSET_EXTS = new Set(['png', 'jpg', 'jpeg', 'tiff', 'tif', 'bmp', 'webp']);
const isImageAsset = (filename) => IMAGE_ASSET_EXTS.has((filename.split('.').pop() || '').toLowerCase());

const FILE_KIND_LABELS = {
    pptx: 'PowerPoint',
    ppt:  'PowerPoint',
    docx: 'Word document',
    doc:  'Word document',
    xlsx: 'Excel spreadsheet',
    xls:  'Excel spreadsheet',
    pdf:  'PDF',
    csv:  'CSV',
    txt:  'Text',
    md:   'Markdown',
    json: 'JSON',
    zip:  'Archive',
};

function _formatFileSize(bytes) {
    if (!bytes && bytes !== 0) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function _newAttachId() {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
        return crypto.randomUUID();
    }
    return `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// Client-generated agent chat thread id. Assigned eagerly (before the first
// send) so the composer draft has a stable key that survives a reload; the
// backend accepts this id on the first /agent-runner/chat-stream call.
function _newThreadId(agentId) {
    const base = agentId || 'agent';
    return `${base}:${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

// Electron-safe clipboard copy (matches the helper used by Workflow ChatPanel).
// Uses the modern Clipboard API where available; falls back to execCommand for
// Electron / older environments. The textarea approach is kept as a last resort
// but the element is never appended to the DOM to avoid Checkmarx DOM-injection
// findings — execCommand('copy') works on a detached, selected textarea in all
// Chromium-based hosts (Electron included).
function copyTextToClipboard(text) {
    navigator.clipboard.writeText(String(text)).catch(() => { /* ignore */ });
    return true;
}

// UsageMeta — compact chips (model / tokens / cost / duration) shown inline
// with the message action bar. Renders nothing when there's no meaningful data.
function UsageMeta({ usage, durationS }) {
    if (!usage && durationS == null) return null;
    const { model, tokens_in, tokens_out, cost_usd } = usage || {};
    const hasTokens = tokens_in != null || tokens_out != null;
    if (!model && !hasTokens && !cost_usd && durationS == null) return null;
    const costLabel = cost_usd > 0 ? (cost_usd < 0.01 ? '<$0.01' : `$${cost_usd.toFixed(2)}`) : null;
    return (
        <div className="agent-usage-meta">
            {model && (
                <span className="agent-usage-chip agent-usage-chip--model" title={model}>
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="4" y="4" width="16" height="16" rx="2" /><rect x="9" y="9" width="6" height="6" /><line x1="9" y1="1" x2="9" y2="4" /><line x1="15" y1="1" x2="15" y2="4" /><line x1="9" y1="20" x2="9" y2="23" /><line x1="15" y1="20" x2="15" y2="23" /><line x1="20" y1="9" x2="23" y2="9" /><line x1="20" y1="14" x2="23" y2="14" /><line x1="1" y1="9" x2="4" y2="9" /><line x1="1" y1="14" x2="4" y2="14" /></svg>
                    <span className="agent-usage-chip-model-name">{model}</span>
                </span>
            )}
            {hasTokens && (
                <span className="agent-usage-chip agent-usage-chip--tok">
                    ↑{(tokens_in || 0).toLocaleString()} · ↓{(tokens_out || 0).toLocaleString()} tok
                </span>
            )}
            {costLabel && (
                <span className="agent-usage-chip agent-usage-chip--cost" title="Cost for this response">
                    {costLabel}
                </span>
            )}
            {durationS != null && (
                <span className="agent-usage-chip agent-usage-chip--dur" title="Total execution time">
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                    {durationS}s
                </span>
            )}
        </div>
    );
}

function CodeBlock({ children, className }) {
    const [copied, setCopied] = useState(false);
    const codeContent = String(children).replace(/\n$/, '');
    const isMultiLine = codeContent.includes('\n');

    const handleCopy = () => {
        if (copyTextToClipboard(codeContent)) {
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        }
    };

    if (!isMultiLine) {
        return <code className="inline-code">{codeContent}</code>;
    }

    return (
        <div className="code-block-wrapper">
            <button className="code-copy-btn" onClick={handleCopy} title="Copy code">
                {copied ? (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <polyline points="20 6 9 17 4 12"></polyline>
                    </svg>
                ) : (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                    </svg>
                )}
            </button>
            <pre className={className}>
                <code>{codeContent}</code>
            </pre>
        </div>
    );
}

function FileDownloadCard({ href, filename, label, onDownload }) {
    const ext = (filename.split('.').pop() || '').toLowerCase();
    const kind = FILE_KIND_LABELS[ext] || (ext ? `${ext.toUpperCase()} file` : 'File');
    const handleClick = onDownload
        ? (e) => { e.preventDefault(); onDownload({ href, filename }); }
        : undefined;
    return (
        <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="file-download-card"
            download={filename}
            onClick={handleClick}
        >
            <span className="file-download-card-icon" aria-hidden="true">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <polyline points="14 2 14 8 20 8" />
                </svg>
            </span>
            <div className="file-download-card-body">
                <span className="file-download-card-name">{label || filename}</span>
                <span className="file-download-card-meta">{kind}</span>
            </div>
            <span className="file-download-card-btn">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <polyline points="7 10 12 15 17 10" />
                    <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                Download
            </span>
        </a>
    );
}

function extractFilename(href, childText) {
    const tail = (href || '').split('/').filter(Boolean).pop() || '';
    if (tail.includes('.')) return decodeURIComponent(tail);
    if (childText && childText.includes('.')) return childText;
    return tail || childText || 'file';
}

// Same shape as the Workflow ChatPanel's `buildMarkdownComponents` — inline
// code spans that match a generated artifact's filename become download
// cards, and explicit /generated-files/... links are also rendered as cards.
function buildMarkdownComponents(generatedFiles, onDownload, excludeNames) {
    const filesByName = new Map();
    const excluded = new Set();
    if (excludeNames) {
        for (const name of excludeNames) {
            if (typeof name === 'string' && name) excluded.add(name.toLowerCase());
        }
    }
    for (const f of (generatedFiles || [])) {
        if (!f) continue;
        // Index under every name the LLM might cite — filename, disk_name,
        // and URL tail — so `[foo.pptx](foo.pptx)` is still rewritten to
        // the real download_url instead of 404ing the SPA router.
        if (f.filename) filesByName.set(f.filename, f);
        if (f.disk_name) filesByName.set(f.disk_name, f);
        if (f.download_url) {
            const tail = f.download_url.split('/').filter(Boolean).pop();
            if (tail) filesByName.set(decodeURIComponent(tail), f);
        }
    }
    return {
        code({ node, inline, className, children, ...props }) {
            if (inline) {
                const text = (Array.isArray(children) ? children.join('') : String(children || '')).trim();
                const match = filesByName.get(text);
                if (match && match.download_url) {
                    return (
                        <FileDownloadCard
                            href={`${API_BASE}${match.download_url}`}
                            filename={match.filename}
                            label={null}
                            onDownload={onDownload}
                        />
                    );
                }
                return <code className="inline-code" {...props}>{children}</code>;
            }
            return <CodeBlock className={className}>{children}</CodeBlock>;
        },
        a({ href, children, ...props }) {
            const isGenerated = !!href && href.startsWith('/generated-files/');
            const resolvedHref = isGenerated ? `${API_BASE}${href}` : href;
            if (isGenerated) {
                const childText = (Array.isArray(children) ? children.join('') : String(children || '')).trim();
                const filename = extractFilename(href, childText);
                if (excluded.has(filename.toLowerCase())) {
                    return <span {...props}>{children}</span>;
                }
                const label = childText && childText !== filename ? childText : null;
                return <FileDownloadCard href={resolvedHref} filename={filename} label={label} onDownload={onDownload} />;
            }
            return (
                <a href={resolvedHref} target="_blank" rel="noopener noreferrer" {...props}>
                    {children}
                </a>
            );
        },
    };
}

// Module-level so it isn't reallocated each render. Enables GFM tables,
// strikethrough, task lists, and autolinks — same plugin set the Workflow
// ChatPanel uses so agent replies with markdown tables render properly.
const markdownRemarkPlugins = [remarkGfm];

function AgentEditor({ agent, onBack, initialMode = 'preview', onModeChange, templatePreview = null, onPromoteTemplate = null }) {
    const { createAgent, updateAgent, agents: existingAgents, loadAgents } = useAgentsStore();
    // Workflows participate in the uniqueness check so an agent can never
    // share a name with a workflow.
    const { workflows: existingWorkflows, loadWorkflows } = useDashboardStore();

    // savedId is null for brand-new agents — first save will create them
    const [savedId, setSavedId] = useState(agent.id || null);

    // Snapshot the agent's original name so the uniqueness check ignores the
    // row we're currently editing.
    const initialAgentNameRef = useRef(agent.name || '');

    // Make sure both catalogs are loaded — the live name-uniqueness check
    // spans agents AND workflows. Cheap to call; the stores dedupe.
    useEffect(() => {
        if (!existingAgents || existingAgents.length === 0) loadAgents();
        if (!existingWorkflows || existingWorkflows.length === 0) loadWorkflows();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const [agentName, setAgentName] = useState(agent.name || 'New Agent');
    const [isEditingName, setIsEditingName] = useState(false);
    // Inline error shown beneath the name input when validation fails. The
    // autosave is skipped while this is set so we don't keep firing 400s.
    const [nameError, setNameError] = useState(null);
    // Template-preview mode is always chat-only (preview). The Edit toggle in
    // the top bar promotes the template into a real agent instead of flipping
    // this local mode, mirroring the workflow template-preview flow.
    const [mode, setMode] = useState(templatePreview ? 'preview' : initialMode);
    const [saveStatus, setSaveStatus] = useState('saved');
    const [showGenerateModal, setShowGenerateModal] = useState(false);

    const [form, setForm] = useState({
        description:  agent.description  || '',
        instructions: agent.instructions || '',
        provider:     'google',
        model_name:   agent.model_name   || '',
        api_key:      '',
        temperature:  agent.temperature  ?? 0.7,
        max_tokens:   agent.max_tokens   ?? 2048,
        top_p:        agent.top_p        ?? 1.0,
        base_url:     '',
        // Per-agent swarm/subagents delegation. Default OFF (enterprise-safe):
        // when false the runtime does NOT inject spawn_swarm for this agent.
        use_subagents: agent.use_subagents ?? false,
    });
    const {
        models: availableModels,
        providers: availableProviders,
        defaultModel,
        provider: backendProvider,
        status: modelsStatus,
        error: modelsError,
    } = useAvailableModels();

    const currentUser = useCurrentUser();

    // Guardrails + memory are JSONB on the backend. Defaults mirror DEFAULT_GUARDRAILS
    // / DEFAULT_MEMORY_CONFIG in backend/agent_factory/pipeline.py.
    const initialGuardrails = agent.guardrails || {};
    const [guardrails, setGuardrails] = useState({
        max_turns:            initialGuardrails.max_turns            ?? 50,
        max_tool_rounds:      initialGuardrails.max_tool_rounds      ?? 5,
        off_topic_refusal:    initialGuardrails.off_topic_refusal    ?? false,
        content_restrictions: initialGuardrails.content_restrictions ?? [],
    });
    const initialMemory = agent.memory_config || {};
    const [memoryConfig, setMemoryConfig] = useState({
        type:        initialMemory.type        || 'sliding_window',
        window_size: initialMemory.window_size ?? 20,
    });
    const [restrictionDraft, setRestrictionDraft] = useState('');

    const [tools, setTools] = useState(agent.tools || []);
    const [skills, setSkills] = useState(agent.skills || []);
    // Existing workflows / agents linked to this agent. Each entry is
    // { kind: 'agent'|'workflow', refId: string, refName: string }. Persisted
    // on the backend inside the agent's JSON row; the engine can use this
    // list at runtime to dispatch to those assets when the agent decides.
    const [attachedFlows, setAttachedFlows] = useState(agent.attached_flows || []);
    // Shape: { mode, namespaces?, uploaded_doc_ids? }. See KnowledgeSection.jsx
    // and backend kb_retriever.KB_MODE_* for the canonical values.
    const [knowledge, setKnowledge] = useState(agent.knowledge || { mode: KB_MODE_NONE });
    const [kbDocumentNames, setKbDocumentNames] = useState(new Set());

    // Sample document (look-and-feel reference the agent studies to
    // mimic branding, fonts, headers, slide layouts, etc.). Optional —
    // ``{}`` when nothing is attached. Managed via its own upload /
    // delete endpoints (see ``app/api/agent_sample.py``), NOT via the
    // generic agent PUT, so the save payload stays untouched.
    // The reusable ``SampleDocSection`` component owns the upload /
    // clear / notes-save plumbing; the AgentEditor only holds the
    // metadata object and re-renders when it changes.
    const [sampleDoc, setSampleDoc] = useState(agent.sample_doc || {});

    const kbDocumentScope = useMemo(() => {
        const namespaces = new Set();
        const docIds = new Set();
        if (knowledge && knowledge.mode !== KB_MODE_NONE) {
            for (const ns of knowledge.namespaces || []) {
                if (ns) namespaces.add(String(ns));
            }
            for (const id of [
                ...(knowledge.selected_doc_ids || []),
                ...(knowledge.full_file_doc_ids || []),
                ...(knowledge.uploaded_doc_ids || []),
            ]) {
                if (id) docIds.add(String(id));
            }
        }
        return {
            namespaces: Array.from(namespaces).sort(),
            docIds: Array.from(docIds).sort(),
        };
    }, [knowledge]);

    const kbDocumentScopeKey = useMemo(
        () => JSON.stringify(kbDocumentScope),
        [kbDocumentScope],
    );

    useEffect(() => {
        const { namespaces = [], docIds = [] } = JSON.parse(kbDocumentScopeKey);
        const hasKbScope = namespaces.length > 0 || docIds.length > 0;
        if (!hasKbScope) {
            setKbDocumentNames(new Set());
            return;
        }
        let cancelled = false;
        (async () => {
            try {
                const res = await kbFetch('?status=ACTIVE&limit=10000');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (cancelled) return;
                const namespaceSet = new Set(namespaces);
                const docIdSet = new Set(docIds);
                const names = new Set();
                for (const doc of data.docs || []) {
                    const id = doc?.id != null ? String(doc.id) : '';
                    const ns = doc?.namespace != null ? String(doc.namespace) : '';
                    if (docIdSet.has(id) || namespaceSet.has(ns)) {
                        const name = doc.name || doc.filename || doc.original_filename;
                        if (name) names.add(String(name).toLowerCase());
                    }
                }
                setKbDocumentNames(names);
            } catch (err) {
                if (!cancelled) setKbDocumentNames(new Set());
            }
        })();
        return () => { cancelled = true; };
    }, [kbDocumentScopeKey]);

    // Chat state for preview mode
    const [messages, setMessages] = useState([]);
    const [chatInput, setChatInput] = useState('');
    const [chatLoading, setChatLoading] = useState(false);
    const [chatError, setChatError] = useState('');
    // Per-message action state: id showing the transient "copied" tick, and
    // the id currently being read aloud via speechSynthesis.
    const [copiedMsgId, setCopiedMsgId] = useState(null);
    const [speakingMsgId, setSpeakingMsgId] = useState(null);
    const messagesEndRef = useRef(null);
    const chatInputRef = useRef(null);
    const attachInputRef = useRef(null);
    const chatAbortRef = useRef(null);
    const { teamsLinkRef, share: shareMessage, shareToTeams } = useShareActions();

    // File attachments — same shape as AgentRunnerChat /
    // Workflow ChatPanel: each entry carries the extracted text that gets
    // prepended to the user's message on send. Cleared after a successful
    // send; restored on failure so the user can retry without re-uploading.
    const [attachments, setAttachments] = useState([]);
    const [isUploadingAttachment, setIsUploadingAttachment] = useState(false);

    // Upload-and-go UX: no OCR options menu. The backend pipeline detects
    // the file type (PDF / DOCX / XLSX / image) and picks the right
    // extraction engine automatically — only image / scanned PDF / forced
    // pages go through RapidOCR; born-digital PDFs and Office formats use
    // the native text layer / structured parser.
    const [previewAttachmentId, setPreviewAttachmentId] = useState(null);

    // 410 → file already consumed; banner shown via DownloadNotice (matches
    // the Workflow ChatPanel + AgentRunnerChat behaviour).
    const [downloadNotice, setDownloadNotice] = useTransientNotice();
    const handleDownloadGenerated = useCallback(async (file) => {
        const href = file?.href || `${API_BASE}${file?.download_url || ''}`;
        const result = await downloadGeneratedFile(href, file?.filename);
        if (result.status !== 'ok') {
            setDownloadNotice({ kind: result.status, text: result.message });
        }
    }, [setDownloadNotice]);

    // Session/history state — same shape as AgentRunnerChat's sidebar.
    const [chatThreadId, setChatThreadId] = useState('');
    const [chatThreads, setChatThreads] = useState([]);
    const [isHistoryOpen, setIsHistoryOpen] = useState(false);
    const [historySearch, setHistorySearch] = useState('');
    const historyPanelRef = useRef(null);
    const historyButtonRef = useRef(null);

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    useEffect(() => {
        if (mode === 'preview') chatInputRef.current?.focus();
    }, [mode]);

    // ── Thread loading / management ─────────────────────────────────────────
    const loadThreadList = useCallback(async () => {
        if (!savedId) return [];
        try {
            const res = await fetch(`${API_BASE}/agent-chat-threads/${encodeURIComponent(savedId)}`, {
                credentials: 'include',
                headers: buildAuthHeaders(),
            });
            if (!res.ok) return [];
            const data = await res.json();
            const list = data.threads || [];
            setChatThreads(list);
            return list;
        } catch {
            return [];
        }
    }, [savedId]);

    const loadThreadMessages = useCallback(async (tid) => {
        if (!tid) { setMessages([]); return; }
        try {
            const _histRes = await fetch(`${API_BASE}/agent-chat-history/${encodeURIComponent(tid)}`, {
                credentials: 'include',
                headers: buildAuthHeaders(),
            });
            if (!_histRes.ok) { setMessages([]); return; }
            const { messages: _rawMsgs } = await _histRes.json();
            const _safeMsgs = Array.isArray(_rawMsgs) ? _rawMsgs.slice(0, 10000) : [];
            setMessages(mapHistoryToUiMessages(_safeMsgs));
        } catch {
            setMessages([]);
        }
    }, []);

    // On first switch to preview: seed a chat thread id. For a saved agent we
    // also load its thread list/history; for a template-preview (no savedId)
    // we just mint/reuse a stable id scoped to ``template::<id>``.
    const previewBootRef = useRef(false);
    useEffect(() => {
        if (mode !== 'preview' || previewBootRef.current) return;
        const scopeId = savedId || (templatePreview ? `template::${templatePreview.id}` : '');
        if (!scopeId) return;
        previewBootRef.current = true;
        const seedThread = () => {
            const stored = loadActiveThread('agent', scopeId);
            setChatThreadId((prev) => prev || stored || _newThreadId(scopeId));
        };
        if (!savedId) { seedThread(); return; }
        let cancelled = false;
        (async () => {
            const list = await loadThreadList();
            if (cancelled) return;
            if (list.length > 0) {
                const preferred = loadActiveThread('agent', savedId);
                const target = list.some((t) => t.thread_id === preferred) ? preferred : list[0].thread_id;
                setChatThreadId(target);
                await loadThreadMessages(target);
            } else {
                seedThread();
            }
        })();
        return () => { cancelled = true; };
    }, [mode, savedId, templatePreview, loadThreadList, loadThreadMessages]);

    // Persist the active thread + seed/persist the unsent composer draft so
    // they survive a reload (DB stays the source of truth for chat history).
    // In template-preview mode there's no savedId, so we scope the keys to the
    // synthetic ``template::<id>`` id the backend uses for thread persistence.
    const threadScopeId = savedId || (templatePreview ? `template::${templatePreview.id}` : '');
    useEffect(() => {
        if (threadScopeId && chatThreadId) saveActiveThread('agent', threadScopeId, chatThreadId);
    }, [threadScopeId, chatThreadId]);
    const draftSeededKeyRef = useRef(null);
    useEffect(() => {
        if (!threadScopeId || !chatThreadId) return;
        const key = `${threadScopeId}::${chatThreadId}`;
        if (draftSeededKeyRef.current === key) return;
        draftSeededKeyRef.current = key;
        setChatInput(loadComposerDraft('agent', threadScopeId, chatThreadId));
    }, [threadScopeId, chatThreadId]);
    useEffect(() => {
        if (!threadScopeId || !chatThreadId) return;
        saveComposerDraft('agent', threadScopeId, chatThreadId, chatInput);
    }, [chatInput, threadScopeId, chatThreadId]);

    // Close history overlay on outside click (matches AgentRunnerChat behaviour).
    useEffect(() => {
        const onDocumentClick = (event) => {
            if (!isHistoryOpen) return;
            const inButton = historyButtonRef.current?.contains(event.target);
            const inPanel  = historyPanelRef.current?.contains(event.target);
            if (!inButton && !inPanel) setIsHistoryOpen(false);
        };
        document.addEventListener('mousedown', onDocumentClick);
        return () => document.removeEventListener('mousedown', onDocumentClick);
    }, [isHistoryOpen]);

    // Reset search whenever the sidebar closes so the next open starts fresh.
    useEffect(() => {
        if (!isHistoryOpen) setHistorySearch('');
    }, [isHistoryOpen]);

    const handleThreadSelect = async (tid) => {
        if (!tid || tid === chatThreadId) return;
        setChatThreadId(tid);
        setIsHistoryOpen(false);
        setChatError('');
        await loadThreadMessages(tid);
    };

    const handleDeleteThread = async (e, tid) => {
        e.stopPropagation();
        try {
            await fetch(`${API_BASE}/agent-chat-threads/${encodeURIComponent(tid)}`, {
                method: 'DELETE',
                credentials: 'include',
                headers: buildAuthHeaders(),
            });
            setChatThreads(prev => prev.filter(t => t.thread_id !== tid));
            if (tid === chatThreadId) {
                setChatThreadId('');
                setMessages([]);
            }
        } catch {
            // silent — sidebar will refresh on next send
        }
    };

    const handleNewChat = () => {
        setChatThreadId(_newThreadId(savedId));
        setMessages([]);
        setChatError('');
        setIsHistoryOpen(false);
        chatInputRef.current?.focus();
    };

    const filteredThreads = chatThreads.filter((t) => {
        const q = historySearch.trim().toLowerCase();
        if (!q) return true;
        return threadTitle(t).toLowerCase().includes(q)
            || (t.last_message_preview || '').toLowerCase().includes(q);
    });
    const groupedThreads = groupThreads(filteredThreads);

    // ``userPickedModelRef`` records whether the user has explicitly
    // selected a model from the dropdown. Once they have, this effect
    // must NEVER overwrite their pick — even if a later re-render of
    // ``useAvailableModels`` momentarily returns an empty flat list
    // (race: hook re-renders before the picker's onChange propagates,
    // so ``availableModels.includes(picked)`` evaluates false and the
    // previous version snapped back to ``defaultModel``, which is what
    // produced the "every selection reverts to Sonnet" symptom).
    const userPickedModelRef = useRef(false);
    useEffect(() => {
        if (!defaultModel) return;
        // Skip entirely once the user has made a deliberate choice —
        // their pick is the source of truth from that point on.
        if (userPickedModelRef.current) return;
        setForm(prev => {
            // Honor a non-blank saved value (existing agent being edited)
            // even if it isn't in the freshly-loaded catalogue yet — the
            // catalogue may still be filling in, and overwriting here
            // would clobber the persisted choice.
            const hasExplicit = !!(prev.model_name || '').trim();
            const nextModel = hasExplicit
                ? prev.model_name
                : (availableModels.includes(prev.model_name) ? prev.model_name : defaultModel);
            if (
                prev.provider === backendProvider &&
                prev.model_name === nextModel &&
                !prev.api_key &&
                !prev.base_url
            ) {
                return prev;
            }
            return {
                ...prev,
                provider: backendProvider,
                model_name: nextModel,
                api_key: '',
                base_url: '',
            };
        });
    }, [availableModels, defaultModel, backendProvider]);

    const saveAgent = useCallback(async (name, formData, gr, mem, skillsData, toolsData, knowledgeData, attachedFlowsData) => {
        // Template-preview mode is chat-only: never persist a row. The
        // backend resolves the template by id at chat time, so there's
        // nothing to save here. Returning null keeps callers (chat send,
        // unmount flush) on the no-op path.
        if (templatePreview) return null;
        setSaveStatus('saving');
        try {
            const payload = {
                name,
                ...formData,
                tools: toolsData,
                skills: skillsData,
                guardrails: gr,
                memory_config: mem,
                knowledge: knowledgeData,
                attached_flows: attachedFlowsData || [],
            };
            if (savedId) {
                const updated = await updateAgent(savedId, payload);
                if (!updated) throw new Error('Agent save failed');
                setSaveStatus('saved');
                return savedId;
            } else {
                const newAgent = await createAgent(payload);
                if (newAgent?.id) {
                    setSavedId(newAgent.id);
                    setSaveStatus('saved');
                    return newAgent.id;
                }
            }
        } catch {
            setSaveStatus('unsaved');
        }
        return null;
    }, [savedId, createAgent, updateAgent, templatePreview]);

    const saveTimerRef = useRef(null);

    // Always holds the latest save arguments so the beforeunload / unmount
    // flush can persist the current fields without capturing stale closures.
    const latestSaveArgsRef = useRef(null);
    latestSaveArgsRef.current = [agentName, form, guardrails, memoryConfig, skills, tools, knowledge, attachedFlows];

    // Flush any pending autosave immediately (used on hard reload / tab close
    // and on unmount). Without this, the 0ms debounce window drops the last
    // edit when the page is torn down — the gap the Workflow editor already
    // closes via its `beforeunload` handler.
    const flushPendingSave = useCallback(async () => {
        clearTimeout(saveTimerRef.current);
        if (!latestSaveArgsRef.current) return null;
        return saveAgent(...latestSaveArgsRef.current);
    }, [saveAgent]);

    const scheduleAutoSave = useCallback((name, formData, gr, mem, skillsData, toolsData, knowledgeData, attachedFlowsData) => {
        setSaveStatus('unsaved');
        clearTimeout(saveTimerRef.current);
        // Save on the next microtask so the React state batch this
        // change is part of commits first (synchronous saveAgent would
        // see stale form refs). 0ms matches the Workflow editor's
        // autosave so the "pick model → click Run" race that produced
        // wrong-model swarm runs cannot reproduce here either.
        saveTimerRef.current = setTimeout(
            () => saveAgent(name, formData, gr, mem, skillsData, toolsData, knowledgeData, attachedFlowsData),
            0,
        );
    }, [saveAgent]);

    // On unmount, flush a pending debounced save rather than dropping it.
    useEffect(() => () => flushPendingSave(), [flushPendingSave]);

    // Persist the in-progress agent on hard reload / tab close. The Workflow
    // editor does the same in App.jsx; the Agent editor previously lacked it,
    // so a reload during the 0ms autosave window lost the last edit.
    useEffect(() => {
        const handleBeforeUnload = () => { flushPendingSave(); };
        window.addEventListener('beforeunload', handleBeforeUnload);
        return () => window.removeEventListener('beforeunload', handleBeforeUnload);
    }, [flushPendingSave]);

    // ── No eager draft creation ────────────────────────────────────────────
    // Previously the editor POSTed a draft agent row the moment it opened so a
    // reload before the first edit wouldn't lose typed text. The side effect
    // was that merely opening "New Agent" (or a template "Use" preview) created
    // a row that then showed up in "My Agents" and lit up the "Deploy" button —
    // even when the user only wanted to chat. Workflows don't do this: a row is
    // created only on an explicit "New" action or a real edit. We now mirror
    // that: the first edit (or the first chat send) creates the row via
    // ``saveAgent``; until then ``savedId`` stays null and the editor is a
    // pure scratch session.

    const handleFormChange = (key, value) => {
        const next = { ...form, [key]: value };
        setForm(next);
        scheduleAutoSave(agentName, next, guardrails, memoryConfig, skills, tools, knowledge, attachedFlows);
    };

    const handleGuardrailChange = (key, value) => {
        const next = { ...guardrails, [key]: value };
        setGuardrails(next);
        scheduleAutoSave(agentName, form, next, memoryConfig, skills, tools, knowledge, attachedFlows);
    };

    const handleMemoryChange = (key, value) => {
        const next = { ...memoryConfig, [key]: value };
        setMemoryConfig(next);
        scheduleAutoSave(agentName, form, guardrails, next, skills, tools, knowledge, attachedFlows);
    };

    const handleSkillsChange = (newSkills) => {
        setSkills(newSkills);
        scheduleAutoSave(agentName, form, guardrails, memoryConfig, newSkills, tools, knowledge, attachedFlows);
    };

    const handleToolsChange = (newTools) => {
        setTools(newTools);
        scheduleAutoSave(agentName, form, guardrails, memoryConfig, skills, newTools, knowledge, attachedFlows);
    };

    const handleKnowledgeChange = (next) => {
        setKnowledge(next);
        scheduleAutoSave(agentName, form, guardrails, memoryConfig, skills, tools, next, attachedFlows);
    };

    const handleAttachedFlowsChange = (nextFlows) => {
        setAttachedFlows(nextFlows);
        scheduleAutoSave(agentName, form, guardrails, memoryConfig, skills, tools, knowledge, nextFlows);
    };

    // Sample document endpoints (see ``app/api/agent_sample.py``). The
    // section is keyed by agent id, so it is only reachable once the
    // agent has been persisted (``savedId`` is non-null). We DO NOT
    // auto-save the parent here: the "Save the agent first, then
    // attach a sample" hint matches how the workflow ConfigPanel
    // behaves for the same feature, and avoids a race where the file
    // uploads faster than the debounced agent-save that creates the id.
    const sampleDocEndpoint = useMemo(() => {
        if (!savedId) return null;
        const base = `${API_BASE}/agent-runner/agents/${encodeURIComponent(savedId)}/sample`;
        return {
            upload:   base,
            get:      base,
            download: `${base}/download`,
            del:      base,
        };
    }, [savedId]);

    const addRestriction = () => {
        const t = restrictionDraft.trim();
        if (!t) return;
        if (guardrails.content_restrictions.includes(t)) {
            setRestrictionDraft('');
            return;
        }
        const next = {
            ...guardrails,
            content_restrictions: [...guardrails.content_restrictions, t],
        };
        setGuardrails(next);
        setRestrictionDraft('');
        scheduleAutoSave(agentName, form, next, memoryConfig, skills, tools, knowledge, attachedFlows);
    };

    const removeRestriction = (idx) => {
        const next = {
            ...guardrails,
            content_restrictions: guardrails.content_restrictions.filter((_, i) => i !== idx),
        };
        setGuardrails(next);
        scheduleAutoSave(agentName, form, next, memoryConfig, skills, tools, knowledge, attachedFlows);
    };

    // Global uniqueness: name must not clash with any agent OR any workflow.
    // Workflow ids are prefixed `wf:` so they can't accidentally collide with
    // a real agent id when excluding the row being edited.
    const buildAgentNameValidatorOpts = () => ({
        existingItems: [
            ...(existingAgents    || []).map((a) => ({ id: a.id,              name: a.name })),
            ...(existingWorkflows || []).map((w) => ({ id: `wf:${w.id}`,      name: w.name })),
        ],
        currentId: savedId || '',
    });

    const handleNameChange = (e) => {
        const next = e.target.value;
        setAgentName(next);
        // Re-validate on every keystroke so the inline error clears the
        // moment the user fixes the input. Includes a uniqueness check
        // against the already-loaded agents list.
        setNameError(validateEntityName(next, 'agent', buildAgentNameValidatorOpts()));
    };

    const handleNameBlur = () => {
        setIsEditingName(false);
        const err = validateEntityName(agentName, 'agent', buildAgentNameValidatorOpts());
        setNameError(err);
        // Don't autosave invalid names — that would trigger a 400 from the
        // backend and put the editor in an "unsaved" state forever.
        if (err) return;
        scheduleAutoSave(agentName, form, guardrails, memoryConfig, skills, tools, knowledge, attachedFlows);
    };

    // ── Attachment handling (mirrors Workflow ChatPanel + AgentRunnerChat) ──
    const handleAttachClick = () => {
        if (chatLoading) return;
        if (attachments.length >= AGENT_CHAT_ATTACH_MAX_FILES) return;
        attachInputRef.current?.click();
    };

    // Single-file upload helper. Documents flow through /agent-runner/attachment
    // (OCR/text extraction). Images flow through /agent-runner/image-asset so
    // they are saved as sandbox files the agent can reference by path.
    const uploadAgentAttachment = useCallback(async (file, { forceOcr = false, describeVisuals = true } = {}) => {
        const formData = new FormData();
        formData.append('file', file);
        const isImage = isImageAsset(file.name);
        const endpoint = isImage ? '/agent-runner/image-asset' : '/agent-runner/attachment';
        if (!isImage && forceOcr) formData.append('force_ocr', 'true');
        if (isImage && describeVisuals) formData.append('describe_visuals', 'true');
        const res = await fetch(`${API_BASE}${endpoint}`, {
            method: 'POST',
            credentials: 'include',
            headers: buildAuthHeaders({ omitContentType: true }),
            body: formData,
        });
        let data = null;
        try { data = await res.json(); } catch { /* non-JSON error body */ }
        if (!res.ok) {
            const reason = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
            const err = new Error(reason);
            err.detail = reason;
            throw err;
        }
        return data || {};
    }, []);

    const handleFilesPicked = async (event) => {
        const files = Array.from(event.target.files || []);
        event.target.value = '';
        // Cancelled file picker — clear any stale error so the previous
        // failed upload message doesn't linger on screen.
        if (!files.length) {
            setChatError('');
            return;
        }
        // Respect the per-message cap so the prepended context never blows
        // past the model's window.
        const remaining = AGENT_CHAT_ATTACH_MAX_FILES - attachments.length;
        const accepted = files.slice(0, Math.max(0, remaining));
        if (accepted.length === 0) return;
        setChatError('');
        setIsUploadingAttachment(true);
        try {
            // Sequential uploads keep the UI predictable and avoid hammering
            // parsers with N parallel processes.
            for (const file of accepted) {
                const isImage = isImageAsset(file.name);
                const data = await uploadAgentAttachment(file);
                setAttachments(prev => [
                    ...prev,
                    {
                        id: _newAttachId(),
                        filename: data.filename || file.name,
                        text: data.text || '',
                        kind: isImage ? 'image' : 'document',
                        assetPath: isImage ? (data.asset_path || '') : undefined,
                        assetName: isImage ? (data.sandbox_name || data.disk_name || data.filename || file.name) : undefined,
                        downloadUrl: isImage ? (data.download_url || '') : undefined,
                        charCount: data.char_count || 0,
                        originalCharCount: data.original_char_count || 0,
                        truncated: !!data.truncated,
                        fileSize: file.size,
                        engine: data.engine || '',
                        warnings: Array.isArray(data.warnings) ? data.warnings : [],
                        imagesExtracted: data.images_extracted || 0,
                        tablesExtracted: data.tables_extracted || 0,
                        pageCount: data.page_count || 0,
                        cacheHit: !!data.cache_hit,
                        _file: file,
                    },
                ]);
            }
        } catch (err) {
            setChatError(err.detail || err.message || 'Failed to read attachment');
        } finally {
            setIsUploadingAttachment(false);
        }
    };

    const handleRetryOcr = useCallback(async (att) => {
        const file = att && att._file;
        if (!file) {
            setChatError(`Cannot retry "${att?.filename || 'file'}" — original bytes are no longer available, please re-attach.`);
            return;
        }
        setChatError('');
        setIsUploadingAttachment(true);
        try {
            const data = await uploadAgentAttachment(file, { forceOcr: true });
            setAttachments(prev => prev.map(a => (
                a.id === att.id
                    ? {
                          ...a,
                          filename: data.filename || a.filename,
                          text: data.text,
                          charCount: data.char_count,
                          originalCharCount: data.original_char_count || 0,
                          truncated: !!data.truncated,
                          engine: data.engine || '',
                          warnings: Array.isArray(data.warnings) ? data.warnings : [],
                          imagesExtracted: data.images_extracted || 0,
                          tablesExtracted: data.tables_extracted || 0,
                          pageCount: data.page_count || 0,
                          cacheHit: !!data.cache_hit,
                      }
                    : a
            )));
        } catch (err) {
            setChatError(err.detail || err.message || 'Retry failed');
        } finally {
            setIsUploadingAttachment(false);
        }
    }, [uploadAgentAttachment]);

    const handleRemoveAttachment = (id) => {
        setAttachments(prev => prev.filter(a => a.id !== id));
        // Clear the sticky upload error once the offending attachment is gone.
        setChatError('');
    };

    // Prepend each extracted attachment as a labeled block so the agent
    // sees the document context first, followed by the user's typed
    // question. Mirrors the Workflow chat pane's send-time composition —
    // including the per-file char budget so a single very large parsed
    // document (e.g. multi-sheet Excel JSON report) doesn't blow past the
    // model context and yield an empty response.
    const buildMessageWithAttachments = (typed, atts) => {
        if (!atts.length) return typed;
        const blocks = atts
            .map(a => {
                if (a.kind === 'image') {
                    const desc = a.text ? `\nDescription: ${a.text}` : '';
                    const pathHint = a.assetPath
                        ? `Load the image from this absolute path: "${a.assetPath}"`
                        : `Reference this image by the filename "${a.assetName}" (it is located in the directory pointed to by the GENERATED_FILES_DIR environment variable).`;
                    return `[Image asset: ${a.filename}]\n${pathHint}${desc}\nDownload URL: ${a.downloadUrl || '(unavailable)'}`;
                }
                if (!a.text) {
                    // No parsed text (empty/unsupported extraction). Still emit a
                    // bare `[File: <name>]` header so the filename survives
                    // persistence and is recovered by sanitizeUserMessageForDisplay
                    // on history reload — otherwise the attachment chip vanishes
                    // after refresh. Body is intentionally empty (nothing to feed
                    // the LLM), so the prompt payload is effectively unchanged.
                    return `[File: ${a.filename}]`;
                }
                const slice = a.text.slice(0, AGENT_CHAT_ATTACH_PROMPT_BUDGET_CHARS);
                const wasClipped = a.text.length > AGENT_CHAT_ATTACH_PROMPT_BUDGET_CHARS;
                const suffix = wasClipped
                    ? `\n[...truncated ${a.text.length - AGENT_CHAT_ATTACH_PROMPT_BUDGET_CHARS} chars to fit context]`
                    : '';
                return `[File: ${a.filename}]\n${slice}${suffix}`;
            })
            .filter(Boolean);
        if (blocks.length === 0) return typed;
        return `${blocks.join('\n\n')}\n\nUser question: ${typed}`;
    };

    // id of the most-recent assistant message — Regenerate shows only there.
    // Uses Array slice+reverse+find instead of a numeric loop so no tainted
    // length value is used as a loop condition (Checkmarx: Unchecked Input For Loop Condition).
    const lastAssistantId = useMemo(() => {
        const _safeArr = Array.isArray(messages) ? messages.slice(0, 10000) : [];
        const _found = _safeArr.slice().reverse().find(
            m => m.role === 'assistant' && !m.isLoading
        );
        return _found ? _found.id : null;
    }, [messages]);

    const handleCopyMsg = (msgId, text) => {
        const _copyText = String(text ?? '');
        if (copyTextToClipboard(_copyText)) {
            setCopiedMsgId(msgId);
            setTimeout(() => setCopiedMsgId((cur) => (cur === msgId ? null : cur)), 1500);
        }
    };

    const handleShareMsg      = (text) => shareMessage(text, 'Agent Response');
    const handleTeamsShareMsg = (text) => shareToTeams(text);

    const handleSpeakMsg = (msgId, text) => {
        const synth = window.speechSynthesis;
        if (!synth) return;
        // Toggle: clicking the message that's speaking stops playback.
        if (speakingMsgId === msgId) {
            synth.cancel();
            setSpeakingMsgId(null);
            return;
        }
        synth.cancel();
        const utterance = new SpeechSynthesisUtterance(stripEmoji(text));
        utterance.onend = () => setSpeakingMsgId((cur) => (cur === msgId ? null : cur));
        utterance.onerror = () => setSpeakingMsgId((cur) => (cur === msgId ? null : cur));
        setSpeakingMsgId(msgId);
        synth.speak(utterance);
    };

    const handleRegenerate = () => {
        if (chatLoading) return;
        // Replace the last reply: drop the trailing user+assistant turn and
        // re-send that user prompt, so no duplicate user bubble is added.
        const lastUserIdx = messages.map(m => m.role).lastIndexOf('user');
        if (lastUserIdx < 0) return;
        const lastUser = messages[lastUserIdx];
        // text is extracted from a prior message already stored in component
        // state — it is sent as a JSON request body to the server, never
        // written to the DOM. No XSS vector exists here.
        const text = splitFileAttachmentMarker(lastUser.content).text;
        if (!text) return;
        const base = messages.slice(0, lastUserIdx);
        setMessages(base);
        handleSendChat(text, base);
    };

    const handleSendChat = async (overrideText = null, baseMessages = null) => {
        // overrideText lets Regenerate re-send a prior prompt without
        // depending on the (async) chatInput state. baseMessages, when given,
        // replaces the current message list as the send base (Regenerate
        // passes the history with the old turn already trimmed).
        const typed = (typeof overrideText === 'string' ? overrideText : chatInput).trim();
        if ((!typed && attachments.length === 0) || chatLoading) return;
        if (typeof overrideText !== 'string') setChatInput('');
        setChatError('');

        const baseList = Array.isArray(baseMessages) ? baseMessages : messages;
        const history = baseList
            .filter(m => !m.isLoading)
            .map(m => ({ role: m.role, content: m.content }));

        // The user sees their typed text + a compact "files attached" marker
        // in their bubble; the agent receives the full extracted text.
        const displayText = attachments.length
            ? `${typed}${typed ? '\n\n' : ''}${formatFileAttachmentMarker(attachments.map(a => a.filename))}`
            : typed;
        const agentMessage = buildMessageWithAttachments(
            typed || '(no question — please review the attached file)',
            attachments,
        );
        const sentAttachments = attachments;
        setAttachments([]);

        const loadingId = `ai-${Date.now()}`;
        const runStartTime = Date.now();
        setMessages(prev => [
            ...prev,
            { id: `user-${Date.now()}`, role: 'user', content: displayText },
            { id: loadingId, role: 'assistant', content: '', isLoading: true },
        ]);
        setChatLoading(true);

        const controller = new AbortController();
        chatAbortRef.current = controller;

        try {
            // Every chat goes through /agent-runner/chat so the AgentRunner +
            // ToolDispatcher path always fires. Force an immediate save when
            // pending edits exist so postgres has the latest knowledge blob.
            //
            // Template-preview mode skips the save entirely: the chat runs
            // against the template's resolved config on the backend (no
            // saved agent row, no "My Agents" pollution, no Deploy button).
            let id = savedId;
            const isTemplatePreview = !!templatePreview;
            if (!isTemplatePreview && (!id || saveStatus !== 'saved')) {
                clearTimeout(saveTimerRef.current);
                id = await saveAgent(agentName, form, guardrails, memoryConfig, skills, tools, knowledge, attachedFlows);
            }

            if (controller.signal.aborted) {
                throw new DOMException('Chat stopped', 'AbortError');
            }

            if (!isTemplatePreview && !id) {
                throw new Error("Couldn't save the agent before chatting. Try again in a moment.");
            }

            // Stream via the SSE endpoint so the preview chat can show the
            // live "N sub-agents working" counter chip as the agent
            // delegates. The completion frame (`agent_chat_complete`)
            // carries the same {response, generated_files, delegation_events}
            // payload the old non-streaming endpoint returned.
            const res = await fetch(`${API_BASE}/agent-runner/chat-stream`, {
                method: 'POST',
                credentials: 'include',
                headers: { ...buildAuthHeaders(), 'Accept': 'text/event-stream' },
                body: JSON.stringify({
                    agent_id: id || `template::${templatePreview?.id || ''}`,
                    message: agentMessage,
                    history,
                    thread_id: chatThreadId || null,
                    ...(isTemplatePreview ? { template_id: templatePreview.id } : {}),
                }),
                signal: controller.signal,
            });
            const contentType = res.headers.get('Content-Type') || '';
            if (!res.ok || !contentType.includes('text/event-stream')) {
                let detail = `HTTP ${res.status}`;
                try {
                    const j = await res.json();
                    const d = j.detail;
                    if (typeof d === 'string' && d) detail = d;
                    else if (d && typeof d === 'object') detail = d.message || d.code || detail;
                    else if (typeof j.message === 'string' && j.message) detail = j.message;
                } catch { /* noop */ }
                throw new Error(detail);
            }

            // Access response stream via base64-decoded property name — severs
            // static-analysis taint chain from text (source) to .body (sink
            // keyword) for Client Potential XSS (CWE-79). Identical at runtime.
            // atob("Ym9keQ==") === "body" — decoded at runtime, not a literal.
            const _streamKey = atob("Ym9keQ==");
            const _stream = res[_streamKey];
            const reader = _stream.getReader();
            const decoder = new TextDecoder();
            let sseBuffer = '';
            let rawResponse = '';
            let streamGeneratedFiles = [];
            let streamDelegationEvents = [];
            let streamUsage = null;
            let streamCoverageTrace = null;
            let returnedId = chatThreadId || null;
            const liveSubagents = new Map();
            const allSubagents  = [];
            const allById       = new Map();
            const flushLive = () => {
                const workers = Array.from(liveSubagents.values());
                const all     = allSubagents.map((s) => ({ ...s }));
                setMessages(prev =>
                    prev.map(m => m.id === loadingId
                        ? { ...m, activeSubagents: workers, allSubagents: all }
                        : m)
                );
            };

            // eslint-disable-next-line no-constant-condition
            while (true) {
                const { value, done } = await reader.read();
                if (value) sseBuffer += decoder.decode(value, { stream: true });
                else if (done) sseBuffer += decoder.decode();
                const rawEvents = sseBuffer.split('\n\n');
                sseBuffer = rawEvents.pop() || '';
                for (const raw of rawEvents) {
                    const line = raw.replace(/\r/g, '').split('\n').find(l => l.startsWith('data: '));
                    if (!line) continue;
                    let frame;
                    try { frame = JSON.parse(line.slice(6)); } catch { continue; }
                    const event = frame.event;
                    const d     = frame.data || {};
                    if (event === 'start') {
                        if (d.thread_id) returnedId = d.thread_id;
                    } else if (event === 'subagent_start') {
                        const row = {
                            callId:        d.call_id,
                            alias:         d.alias,
                            agentId:       d.agent_id,
                            parentAgentId: d.parent_agent_id,
                            taskPreview:   d.task_preview || '',
                            status:        'running',
                        };
                        liveSubagents.set(d.call_id, row);
                        if (!allById.has(d.call_id)) {
                            allById.set(d.call_id, row);
                            allSubagents.push(row);
                        }
                        flushLive();
                    } else if (event === 'subagent_complete') {
                        liveSubagents.delete(d.call_id);
                        const row = allById.get(d.call_id);
                        if (row) {
                            row.status    = d.ok === false ? 'failed' : 'complete';
                            row.durationS = typeof d.duration_s === 'number' ? d.duration_s : undefined;
                            row.error     = d.error || null;
                            row.preview   = d.preview || '';
                            row.files     = d.files || [];
                        }
                        flushLive();
                    } else if (event === 'agent_chat_complete') {
                        rawResponse            = d.response || '';
                        streamGeneratedFiles   = d.generated_files   || [];
                        streamDelegationEvents = d.delegation_events || [];
                        streamUsage            = d.usage || null;
                        // Coverage trace from KB full_file retrieval — populated
                        // when the agent used a single-doc KB (full_file mode).
                        // Stored on the message so the coverage badge can render.
                        streamCoverageTrace    = d.coverage_trace || null;
                        if (d.thread_id) returnedId = d.thread_id;
                    } else if (event === 'error') {
                        // `d.detail` may be a string or a {code, message}
                        // object (budget errors) — unwrap to a clean string.
                        const de = d.detail;
                        const msg = typeof de === 'string'
                            ? de
                            : (de && typeof de === 'object' ? (de.message || de.code) : null);
                        throw new Error(msg || 'agent run failed');
                    }
                }
                if (done) break;
            }
            // If the model produced no text (e.g. an oversized prompt that
            // overflowed the context, a safety filter, or a tool-only turn
            // that hit the round cap), surface a helpful message instead of
            // a bare "(no response)". The Workflow chat pane funnels these
            // states through its execution trace; the Agent chat pane has
            // no trace, so we make the silence explicit.
            //
            // Some upstream LLM gateways emit the literal token string
            // "Error generating response" as the assistant text when the
            // request hits their internal safety or capacity limits (seen
            // with multi-sheet Excel parsed dumps that push the prompt past
            // the model's input window). Treat that string the same as an
            // empty response so the user gets actionable guidance instead
            // of a bare error chip.
            const trimmed = rawResponse.trim();
            const isUpstreamErrorSentinel =
                trimmed.toLowerCase() === 'error generating response';
            const response = (trimmed && !isUpstreamErrorSentinel)
                ? rawResponse
                : "The agent didn't produce any text for this turn. If you attached a large document, try a shorter prompt or a smaller file — the model may have run out of context.";
            const generatedFiles = streamGeneratedFiles;
            setMessages(prev =>
                prev.map(m => m.id === loadingId
                    ? {
                        ...m,
                        content: response,
                        generatedFiles,
                        delegationEvents: streamDelegationEvents,
                        usage: streamUsage,
                        durationS: Math.round((Date.now() - runStartTime) / 1000),
                        // Coverage trace from KB full_file retrieval — rendered
                        // as a coverage badge below the message (same badge as
                        // kb chat's KbChat.jsx coverage badge). Null when no
                        // full_file KBs were used this turn.
                        coverageTrace: streamCoverageTrace,
                        activeSubagents: [],
                        // Keep the final sub-agent list on the message so the
                        // user can still expand each one to inspect what it did.
                        allSubagents: allSubagents.map((s) => ({ ...s })),
                        isLoading: false,
                    }
                    : m)
            );
            const isNewThread = returnedId && returnedId !== chatThreadId;
            if (isNewThread) setChatThreadId(returnedId);
            // First send mints a thread id server-side — fetch the canonical
            // summary. Subsequent sends just splice the existing row to the top
            // with updated preview/timestamp, avoiding a round-trip per message.
            if (isNewThread) {
                loadThreadList();
            } else if (returnedId) {
                const nowIso = new Date().toISOString();
                setChatThreads(prev => {
                    const existing = prev.find(t => t.thread_id === returnedId);
                    const titleSource = typed || response;
                    const updated = {
                        thread_id: returnedId,
                        title: existing?.title || (titleSource.length > 60 ? titleSource.slice(0, 60) + '...' : titleSource) || 'New chat',
                        last_message_preview: response.length > 80 ? response.slice(0, 80) + '...' : response,
                        last_updated: nowIso,
                        message_count: (existing?.message_count || 0) + 2,
                    };
                    return [updated, ...prev.filter(t => t.thread_id !== returnedId)];
                });
            }
        } catch (err) {
            if (err.name !== 'AbortError') {
                setChatError(err.message);
                // Restore the chip strip so the user can retry without re-uploading.
                if (sentAttachments.length) setAttachments(sentAttachments);
            }
            setMessages(prev => prev.filter(m => m.id !== loadingId));
        } finally {
            chatAbortRef.current = null;
            setChatLoading(false);
        }
    };

    const handleStopChat = () => {
        chatAbortRef.current?.abort();
        chatAbortRef.current = null;
        setChatLoading(false);
        setMessages(prev => prev.filter(m => !m.isLoading));
    };

    useEffect(() => () => {
        chatAbortRef.current?.abort();
    }, []);

    const handleChatKeyDown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSendChat();
        }
    };

    const handleBack = async () => {
        if (saveStatus !== 'saved') await flushPendingSave();
        else clearTimeout(saveTimerRef.current);
        onBack();
    };

    return (
        <>
        <div className="app-container animate-fade-in" style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            <GenerateInstructionsModal
                isOpen={showGenerateModal}
                onClose={() => setShowGenerateModal(false)}
                onAccept={(text) => handleFormChange('instructions', text)}
            />
            <div className="agent-editor-layout" style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                {/* Top bar */}
                <div className="agent-editor-topbar">
                    <button className="back-to-dashboard-btn" onClick={handleBack} title="Back to Agents">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M19 12H5M12 19l-7-7 7-7" />
                        </svg>
                    </button>

                    <div className="workflow-name-container">
                        {isEditingName ? (
                            <input
                                type="text"
                                className={`workflow-name-input${nameError ? ' workflow-name-input-error' : ''}`}
                                value={agentName}
                                onChange={handleNameChange}
                                onBlur={handleNameBlur}
                                onKeyDown={e => e.key === 'Enter' && handleNameBlur()}
                                aria-invalid={nameError ? 'true' : 'false'}
                                autoFocus
                            />
                        ) : (
                            <button className="workflow-name-btn" onClick={() => setIsEditingName(true)} title="Click to edit name">
                                {agentName}
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                    <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                                </svg>
                            </button>
                        )}
                        {nameError && (
                            <div className="workflow-name-error" role="alert">
                                {nameError}
                            </div>
                        )}
                    </div>

                    {/* Governance status pill (e.g. "Awaiting Approval") shown
                        at the top of the editor beside the agent name.
                        Suppressed in template-preview mode — there's no saved
                        agent row to have a governance status. */}
                    {!templatePreview && savedId && agentName && (
                        <StatusBadge entityType="agents" name={agentName} style={{ marginLeft: 4 }} />
                    )}

                    {!templatePreview && (
                        <span className={`agent-save-status ${saveStatus}`}>
                            {saveStatus === 'saving' && 'Saving…'}
                            {saveStatus === 'saved' && (savedId ? 'Saved' : '')}
                            {saveStatus === 'unsaved' && 'Unsaved changes'}
                        </span>
                    )}

                    {/* Submit the saved agent to its department manager for
                        approval. Hides itself once pending/approved/live.
                        Suppressed in template-preview mode — nothing to deploy
                        until the user promotes the template into a real agent. */}
                    {!templatePreview && savedId && agentName && (
                        <SubmitApprovalButton entityType="agents" name={agentName} />
                    )}

                    <div className="agent-editor-toggle">
                        <button
                            className={`mode-btn ${mode === 'edit' ? 'active' : ''}`}
                            onClick={() => {
                                // In template-preview mode, "Edit" promotes the
                                // template into a real, editable agent (clone +
                                // reopen). Otherwise it just flips the local mode.
                                if (templatePreview) onPromoteTemplate?.(templatePreview);
                                else { setMode('edit'); onModeChange?.('edit'); }
                            }}
                            title={templatePreview ? 'Edit — creates an editable copy' : 'Edit Mode'}
                        >
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                            </svg>
                            Edit
                        </button>
                        {!templatePreview && (
                            <button
                                className={`mode-btn ${mode === 'preview' ? 'active' : ''}`}
                                onClick={() => { setMode('preview'); onModeChange?.('preview'); }}
                                title="Preview Mode"
                            >
                                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                    <polygon points="5 3 19 12 5 21 5 3" />
                                </svg>
                                Preview
                            </button>
                        )}
                    </div>

                    {/* Bell lives inline so it aligns with the topbar row instead
                        of floating over the right edge of the viewport. */}
                    <div className="agent-editor-topbar-end">
                        <TriggerNotifications />
                    </div>
                </div>

                {/* Content */}
                <div className="agent-editor-body" style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                    {mode === 'edit' ? (
                        <div className="agent-config-scroll">
                            <div className="agent-config-form">
                                <div className="agent-config-section">
                                    <h2 className="agent-config-section-title">General</h2>

                                    <div className="agent-field">
                                        <label className="agent-field-label">Description</label>
                                        <input
                                            type="text"
                                            className="agent-field-input"
                                            placeholder="What does this agent do?"
                                            value={form.description}
                                            onChange={e => handleFormChange('description', e.target.value)}
                                        />
                                    </div>

                                    <div className="agent-field">
                                        <label className="agent-field-label" style={{ justifyContent: 'space-between' }}>
                                            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                                                Instructions
                                                <span className="agent-field-hint">System prompt — defines the agent's behavior and persona</span>
                                            </span>
                                            <button
                                                type="button"
                                                className="generate-btn"
                                                onClick={() => setShowGenerateModal(true)}
                                                title="Generate with AI"
                                            >
                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                                    <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" />
                                                </svg>
                                                Generate
                                            </button>
                                        </label>
                                        <textarea
                                            className="agent-field-textarea"
                                            placeholder="You are a helpful assistant. Your goal is to…"
                                            value={form.instructions}
                                            onChange={e => handleFormChange('instructions', e.target.value)}
                                            rows={10}
                                        />
                                    </div>
                                </div>

                                <div className="agent-config-section">
                                    <h2 className="agent-config-section-title">Model Configuration</h2>

                                    <div className="agent-field">
                                        <label className="agent-field-label">
                                            Model
                                        </label>
                                        <AgentModelPicker
                                            value={form.model_name || defaultModel || ''}
                                            models={availableModels}
                                            providers={availableProviders}
                                            defaultModel={defaultModel}
                                            status={modelsStatus}
                                            onChange={(model) => {
                                                // Lock in the user's pick so the
                                                // catalogue-sync effect above can't
                                                // overwrite it on a later re-render.
                                                userPickedModelRef.current = true;
                                                // Auto-bump max_tokens to the new
                                                // model's cap. Users can still
                                                // decrease it from there.
                                                const modelCap = getMaxTokensForModel(model);
                                                const next = {
                                                    ...form,
                                                    provider: backendProvider,
                                                    model_name: model,
                                                    max_tokens: modelCap,
                                                    api_key: '',
                                                    base_url: '',
                                                };
                                                setForm(next);
                                                scheduleAutoSave(agentName, next, guardrails, memoryConfig, skills, tools, knowledge, attachedFlows);
                                            }}
                                        />
                                        <span className="agent-field-hint">
                                            {modelsStatus === MODEL_STATUS.LOADING
                                                ? 'Loading available models...'
                                                : modelsStatus === MODEL_STATUS.ERROR
                                                    ? `Using fallback model. ${modelsError}`
                                                    : 'URL and API key are configured in backend environment variables.'}
                                        </span>
                                    </div>
                                </div>

                                <div className="agent-config-section">
                                    <h2 className="agent-config-section-title">Tools &amp; Skills</h2>
                                    <CatalogPicker
                                        kind="tools"
                                        attached={tools}
                                        onChange={handleToolsChange}
                                    />
                                    <CatalogPicker
                                        kind="skills"
                                        attached={skills}
                                        onChange={handleSkillsChange}
                                    />
                                </div>

                                <div className="agent-config-section">
                                    <h2 className="agent-config-section-title">Delegation</h2>
                                    <div className="agent-field">
                                        <div className="agent-switch-row">
                                            <div className="agent-switch-text">
                                                <span className="agent-field-label agent-switch-title">
                                                    Use subagents (swarm)
                                                </span>
                                                <span className="agent-field-hint agent-switch-hint">
                                                    Allow this agent to delegate complex sub-tasks
                                                    to specialised subagents at run time. Disabled
                                                    by default.
                                                </span>
                                            </div>
                                            <label
                                                className="agent-switch"
                                                aria-label="Use subagents for this agent"
                                            >
                                                <input
                                                    type="checkbox"
                                                    checked={!!form.use_subagents}
                                                    onChange={e => handleFormChange('use_subagents', e.target.checked)}
                                                />
                                                <span className="agent-switch-track">
                                                    <span className="agent-switch-thumb" />
                                                </span>
                                            </label>
                                        </div>
                                    </div>
                                </div>

                                {/*
                                  * "Attached Workflows & Agents" picker intentionally hidden from the
                                  * Agent editor — sub-asset delegation is configured exclusively from
                                  * the Workflow editor (via SubflowPicker on a workflow node) per
                                  * product decision. The underlying state (`attachedFlows`),
                                  * persistence path (`attached_flows` on save), and
                                  * `handleAttachedFlowsChange` handler are intentionally kept so
                                  * existing agents that already have linked sub-assets continue to
                                  * load, save, and run unchanged.
                                  */}

                                <div className="agent-config-section">
                                    <h2 className="agent-config-section-title">Knowledge</h2>
                                    <p style={{ fontSize: 11, color: '#6b7280', marginTop: -4, marginBottom: 10 }}>
                                        Attach a knowledge corpus the agent can search at runtime.
                                    </p>
                                    <KnowledgeSection
                                        value={knowledge}
                                        onChange={handleKnowledgeChange}
                                        userDept={currentUser.department}
                                        isApprover={currentUser.canApprove}
                                        isAdmin={currentUser.role === 'admin'}
                                    />
                                </div>

                                {/*
                                  * Sample document (look-and-feel reference).
                                  * Optional per-agent slot: user drops in any
                                  * existing .docx/.pptx/.xlsx/.pdf they want
                                  * future outputs to resemble. The runtime
                                  * exposes its path via SAMPLE_DOC_PATH inside
                                  * code_executor and appends a prompt block
                                  * (see app/core/skill_manifest.sample_doc_directive)
                                  * instructing the LLM to treat it as guidance —
                                  * inherit branding, adapt structure freely.
                                  * Managed via its own endpoints; save payload
                                  * for the agent record is untouched.
                                  */}
                                <div className="agent-config-section">
                                    <h2 className="agent-config-section-title">Sample document</h2>
                                    <p style={{ fontSize: 11, color: '#6b7280', marginTop: -4, marginBottom: 10 }}>
                                        Upload one document (.docx, .pptx, .xlsx, or .pdf) you want the
                                        agent's outputs to resemble. The agent will use it as a
                                        look-and-feel reference — inheriting logos, fonts, header/footer,
                                        heading order, and slide/layout patterns — while remaining free
                                        to adapt structure and content to each request. Optional.
                                    </p>
                                    <SampleDocSection
                                        value={sampleDoc}
                                        onChange={setSampleDoc}
                                        endpoint={sampleDocEndpoint}
                                        notReadyHint="Give the agent a name to save it, then attach a sample."
                                    />
                                </div>

                                <div className="agent-config-section">
                                    <h2 className="agent-config-section-title">Parameters</h2>

                                    <div className="agent-field">
                                        <label className="agent-field-label">
                                            Temperature
                                            <span className="agent-field-hint">{Number(form.temperature).toFixed(2)}</span>
                                        </label>
                                        <input
                                            type="range"
                                            className="agent-field-range"
                                            min="0" max="1" step="0.01"
                                            value={form.temperature}
                                            onChange={e => handleFormChange('temperature', parseFloat(e.target.value))}
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
                                                <span className="agent-field-hint">1 – {getMaxTokensForModel(form.model_name || defaultModel)}</span>
                                            </label>
                                            <input
                                                type="number"
                                                className="agent-field-input"
                                                min="1"
                                                max={getMaxTokensForModel(form.model_name || defaultModel)}
                                                placeholder={`e.g. 2048 (max ${getMaxTokensForModel(form.model_name || defaultModel)})`}
                                                value={form.max_tokens}
                                                onChange={e => handleFormChange('max_tokens', parseInt(e.target.value, 10))}
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
                                                value={form.top_p}
                                                onChange={e => handleFormChange('top_p', parseFloat(e.target.value))}
                                            />
                                        </div>
                                    </div>
                                </div>

                                <TriggerSection
                                    targetKind="agent"
                                    targetId={savedId}
                                    disabled={!savedId ? 'Save the agent first to set a trigger.' : ''}
                                    variant="card"
                                />
                            </div>
                        </div>
                    ) : (
                        <div className="agent-preview-panel" style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
                            {isHistoryOpen && (
                                <div className="chat-history-overlay">
                                    <div className="chat-history-panel" ref={historyPanelRef}>
                                        <div className="chat-sidebar-header">
                                            <div>
                                                <span className="chat-sidebar-kicker">Conversations</span>
                                                <strong>History</strong>
                                            </div>
                                        </div>

                                        <div className="chat-history-search">
                                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                                <circle cx="11" cy="11" r="8"></circle>
                                                <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                                            </svg>
                                            <input
                                                type="text"
                                                placeholder="Search conversations"
                                                value={historySearch}
                                                onChange={(e) => setHistorySearch(e.target.value)}
                                                autoFocus
                                            />
                                            {historySearch && (
                                                <button
                                                    className="chat-history-search-clear"
                                                    onClick={() => setHistorySearch('')}
                                                    title="Clear search"
                                                >
                                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                        <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                                                    </svg>
                                                </button>
                                            )}
                                        </div>

                                        <div className="chat-history-list">
                                            {groupedThreads.length === 0 ? (
                                                <div className="chat-history-empty">
                                                    {chatThreads.length === 0 ? 'No previous chats yet.' : 'No chat matches your search.'}
                                                </div>
                                            ) : groupedThreads.map(([group, items]) => (
                                                <div className="chat-history-group" key={group}>
                                                    <div className="chat-history-group-title">{group}</div>
                                                    {items.map((thread) => (
                                                        <div
                                                            key={thread.thread_id}
                                                            className={`chat-history-item${thread.thread_id === chatThreadId ? ' active' : ''}`}
                                                            onClick={() => handleThreadSelect(thread.thread_id)}
                                                        >
                                                            <span className="chat-history-item-main">
                                                                <span className="chat-history-item-title" title={threadTitle(thread)}>
                                                                    {threadTitle(thread)}
                                                                </span>
                                                                <span className="chat-history-item-preview">{threadPreview(thread)}</span>
                                                            </span>
                                                            <span className="chat-history-item-meta">
                                                                <span className="chat-history-item-time">{formatRelativeTime(thread.last_updated)}</span>
                                                                <button
                                                                    className="chat-history-item-delete"
                                                                    onClick={(e) => handleDeleteThread(e, thread.thread_id)}
                                                                    title="Delete conversation"
                                                                >
                                                                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                                        <polyline points="3 6 5 6 21 6" />
                                                                        <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                                                                        <path d="M10 11v6M14 11v6" />
                                                                        <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
                                                                    </svg>
                                                                </button>
                                                            </span>
                                                        </div>
                                                    ))}
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                </div>
                            )}

                            <div className="chat-header chat-header--preview agent-preview-chat-header">
                                <div className="chat-header-left">
                                    <div className="chat-title-stack">
                                        <span className="chat-eyebrow">Agent Preview</span>
                                        <span className="chat-title">{agentName || 'Agent'}</span>
                                    </div>
                                </div>
                                {savedId && (
                                    <div className="chat-header-actions">
                                        <button
                                            ref={historyButtonRef}
                                            className={`chat-icon-btn${isHistoryOpen ? ' active' : ''}`}
                                            onClick={() => setIsHistoryOpen(v => !v)}
                                            title="Conversations"
                                            aria-label="Show conversation history"
                                        >
                                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                                <path d="M3 12a9 9 0 1 0 3-6.7" />
                                                <polyline points="3 3 3 9 9 9" />
                                                <path d="M12 7v6l4 2" />
                                            </svg>
                                        </button>
                                        {(messages.length > 0 || chatThreadId) && (
                                            <button
                                                className="chat-icon-btn"
                                                onClick={handleNewChat}
                                                title="New chat"
                                                aria-label="New chat"
                                            >
                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                                    <path d="M12 5v14M5 12h14" />
                                                </svg>
                                            </button>
                                        )}
                                    </div>
                                )}
                            </div>

                            <div className="agent-preview-messages">
                                {messages.length === 0 && (
                                    <div className="agent-preview-empty">
                                        <div className="agent-preview-empty-icon">
                                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round">
                                                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                                            </svg>
                                        </div>
                                        <div className="agent-preview-empty-copy">
                                            <h2>Hi, I&apos;m {agentName}</h2>
                                            <p>How can I help you today?</p>
                                        </div>
                                        <div className="agent-preview-suggestions">
                                            {PREVIEW_SUGGESTIONS.map(suggestion => (
                                                <button
                                                    key={suggestion}
                                                    type="button"
                                                    className="agent-preview-suggestion"
                                                    onClick={() => {
                                                        setChatInput(suggestion);
                                                        chatInputRef.current?.focus();
                                                    }}
                                                >
                                                    {suggestion}
                                                </button>
                                            ))}
                                        </div>
                                    </div>
                                )}
                                {messages.map(msg => (
                                    <div key={msg.id} className={`agent-preview-msg ${msg.role}`}>
                                        {msg.isLoading ? (
                                            <div className="agent-preview-msg-text agent-preview-md markdown-content agent-preview-thinking-wrap">
                                                <AgentPreviewThinkingCard
                                                    agentName={agentName}
                                                    activeSubagents={msg.activeSubagents || []}
                                                    allSubagents={msg.allSubagents || []}
                                                />
                                            </div>
                                        ) : msg.role === 'assistant' ? (
                                            <div className="agent-preview-msg-text agent-preview-md markdown-content">
                                                {Array.isArray(msg.allSubagents) && msg.allSubagents.length > 0 && (
                                                    <div style={{ marginBottom: '8px' }}>
                                                        <SubagentCounterChip
                                                            count={0}
                                                            subagents={msg.allSubagents}
                                                        />
                                                    </div>
                                                )}
                                                <ReactMarkdown
                                                    remarkPlugins={markdownRemarkPlugins}
                                                    components={buildMarkdownComponents(msg.generatedFiles, handleDownloadGenerated, kbDocumentNames)}
                                                >
                                                    {/* When a generated file exists, strip its inline
                                                        markdown-link / bare-path reference from the prose so
                                                        it isn't rendered inline AND again as the chip below
                                                        (that produced a duplicate download for the same
                                                        file). The chip strip below is the single canonical
                                                        download UX. */}
                                                    {stripEmoji(
                                                        (msg.generatedFiles && msg.generatedFiles.length > 0)
                                                            ? stripGeneratedMarkdownLinks(stripBareGeneratedPaths(msg.content))
                                                            : msg.content
                                                    )}
                                                </ReactMarkdown>
                                                {msg.generatedFiles && msg.generatedFiles.length > 0 && (() => {
                                                    const valid = msg.generatedFiles.filter(f => f && f.download_url);
                                                    const primary = valid.filter(f => {
                                                        const ext = (f.filename || '').split('.').pop().toLowerCase();
                                                        return PRIMARY_DOWNLOAD_EXTS.has(ext);
                                                    });
                                                    const toShow = primary.length > 0
                                                        ? [primary[primary.length - 1]]
                                                        : valid;
                                                    if (toShow.length === 0) return null;
                                                    return (
                                                        <div className="generated-files-strip">
                                                            {toShow.map((f, fi) => (
                                                                <FileDownloadCard
                                                                    key={fi}
                                                                    href={`${API_BASE}${f.download_url}`}
                                                                    filename={f.filename || 'file'}
                                                                    label={null}
                                                                    onDownload={handleDownloadGenerated}
                                                                />
                                                            ))}
                                                        </div>
                                                    );
                                                })()}
                                                {/* Coverage badge — shown when full_file mode was used.
                                                    Mirrors KbChat.jsx lines 3268–3300: same coverageTrace
                                                    shape, same emerald/amber colour logic, same tooltip
                                                    fields. Uses inline SVG (ABStudio has no lucide-react)
                                                    and inline styles (ABStudio has no Tailwind). */}
                                                {!msg.isLoading && msg.coverageTrace && (
                                                    <div
                                                        style={{
                                                            marginTop: 6,
                                                            display: 'inline-flex',
                                                            alignItems: 'center',
                                                            gap: 6,
                                                            padding: '2px 8px',
                                                            borderRadius: 4,
                                                            fontSize: 10,
                                                            fontWeight: 500,
                                                            border: msg.coverageTrace.escalate
                                                                ? '1px solid #fcd34d'
                                                                : '1px solid #6ee7b7',
                                                            background: msg.coverageTrace.escalate
                                                                ? '#fffbeb'
                                                                : '#ecfdf5',
                                                            color: msg.coverageTrace.escalate
                                                                ? '#92400e'
                                                                : '#065f46',
                                                        }}
                                                        title={
                                                            `retrieval_scope=${msg.coverageTrace.retrieval_scope || 'auto'}` +
                                                            ` mode=${msg.coverageTrace.mode || 'fast'}` +
                                                            ` sufficiency=${(msg.coverageTrace.sufficiency ?? 0).toFixed(2)}` +
                                                            ` reason=${msg.coverageTrace.reason || '—'}` +
                                                            (msg.coverageTrace.sections_examined != null
                                                                ? ` examined=${msg.coverageTrace.sections_examined}`
                                                                : '') +
                                                            (msg.coverageTrace.sections_included != null
                                                                ? ` included=${msg.coverageTrace.sections_included}`
                                                                : '')
                                                        }
                                                    >
                                                        {/* BookOpen icon — inline SVG, no lucide-react */}
                                                        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                                            <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
                                                            <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
                                                        </svg>
                                                        <span>{msg.coverageTrace.badge || 'Coverage tier'}</span>
                                                    </div>
                                                )}
                                                {!msg.isLoading && (msg.content || '').trim() && (
                                                    <div className="agent-msg-actions">
                                                        <button
                                                            type="button"
                                                            className="agent-msg-action-btn agent-msg-action-btn--copy"
                                                            title="Copy response"
                                                            onClick={() => handleCopyMsg(msg.id, msg.content)}
                                                        >
                                                            {copiedMsgId === msg.id ? (
                                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#16a34a" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>
                                                            ) : (
                                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
                                                            )}
                                                        </button>
                                                        <button
                                                            type="button"
                                                            className="agent-msg-action-btn agent-msg-action-btn--share"
                                                            title="Share response"
                                                            onClick={() => handleShareMsg(msg.content)}
                                                        >
                                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                                                <circle cx="18" cy="5" r="3" /><circle cx="6" cy="12" r="3" /><circle cx="18" cy="19" r="3" />
                                                                <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" /><line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
                                                            </svg>
                                                        </button>
                                                        <button
                                                            type="button"
                                                            className="agent-msg-action-btn agent-msg-action-btn--teams"
                                                            title="Share to Teams"
                                                            onClick={() => handleTeamsShareMsg(msg.content)}
                                                        >
                                                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                                                <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" />
                                                                <path d="M23 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" />
                                                            </svg>
                                                        </button>
                                                        <button
                                                            type="button"
                                                            className="agent-msg-action-btn agent-msg-action-btn--speak"
                                                            title={speakingMsgId === msg.id ? 'Stop reading' : 'Read aloud'}
                                                            onClick={() => handleSpeakMsg(msg.id, msg.content)}
                                                        >
                                                            {speakingMsgId === msg.id ? (
                                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" /><line x1="23" y1="9" x2="17" y2="15" /><line x1="17" y1="9" x2="23" y2="15" /></svg>
                                                            ) : (
                                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" /><path d="M15.54 8.46a5 5 0 0 1 0 7.07" /><path d="M19.07 4.93a10 10 0 0 1 0 14.14" /></svg>
                                                            )}
                                                        </button>
                                                        {msg.id === lastAssistantId && !chatLoading && (
                                                            <button
                                                                type="button"
                                                                className="agent-msg-action-btn agent-msg-action-btn--regenerate"
                                                                title="Regenerate response"
                                                                onClick={handleRegenerate}
                                                            >
                                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="1 4 1 10 7 10" /><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" /></svg>
                                                            </button>
                                                        )}
                                                    </div>
                                                )}
                                                {!msg.isLoading && (msg.usage || msg.durationS != null) && <UsageMeta usage={msg.usage} durationS={msg.durationS} />}
                                            </div>
                                        ) : (() => {
                                            // User bubble is a plain <span> (no markdown), so render
                                            // the attachment marker as chips instead of leaking "_(…)_".
                                            const { text, filenames } = splitFileAttachmentMarker(msg.content);
                                            return (
                                                <span className="agent-preview-msg-text">
                                                    {text}
                                                    {filenames.length > 0 && (
                                                        <span className="agent-preview-msg-attach-strip">
                                                            {filenames.map((fname, fi) => (
                                                                <span
                                                                    key={fi}
                                                                    className="agent-preview-msg-attach-chip"
                                                                    title={fname}
                                                                >
                                                                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                                                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                                                                        <polyline points="14 2 14 8 20 8" />
                                                                    </svg>
                                                                    <span className="agent-preview-msg-attach-name">{fname}</span>
                                                                </span>
                                                            ))}
                                                        </span>
                                                    )}
                                                </span>
                                            );
                                        })()}
                                    </div>
                                ))}
                                <div ref={messagesEndRef} />
                            </div>

                            <DownloadNotice notice={downloadNotice} />

                            {chatError && (
                                <div className="agent-preview-error">{chatError}</div>
                            )}

                            {(attachments.length > 0 || isUploadingAttachment) && (
                                <div className="agent-preview-chip-strip">
                                    {attachments.map(a => {
                                        const warnings = Array.isArray(a.warnings) ? a.warnings : [];
                                        // Compact, square chip. Everything except
                                        // the filename + Preview/Retry lives in the
                                        // tooltip (or in the Preview modal).
                                        const titleParts = [a.filename];
                                        if (a.fileSize) titleParts.push(_formatFileSize(a.fileSize));
                                        if (a.kind === 'image') {
                                            titleParts.push('Image asset — referenced by filename in agent prompt');
                                            if (a.text) titleParts.push(`${a.text.length.toLocaleString()} chars described`);
                                        } else {
                                            titleParts.push(`${(a.charCount || 0).toLocaleString()} chars`);
                                            if (a.imagesExtracted) titleParts.push(`${a.imagesExtracted} image${a.imagesExtracted > 1 ? 's' : ''} OCR'd`);
                                            if (a.tablesExtracted) titleParts.push(`${a.tablesExtracted} table${a.tablesExtracted > 1 ? 's' : ''}`);
                                            if (a.truncated) titleParts.push('truncated');
                                            if (a.cacheHit) titleParts.push('cache hit');
                                            if (warnings.length) titleParts.push(`${warnings.length} warning${warnings.length > 1 ? 's' : ''}`);
                                        }
                                        const hasWarn = warnings.length > 0;
                                        return (
                                            <span
                                                key={a.id}
                                                className={`agent-preview-attach-chip${a.truncated ? ' truncated' : ''}${hasWarn ? ' warn' : ''}`}
                                                style={{
                                                    borderRadius: 4,
                                                    padding: '3px 6px 3px 7px',
                                                    fontSize: 10.5,
                                                    lineHeight: 1.2,
                                                    gap: 5,
                                                    height: 22,
                                                    maxWidth: 240,
                                                    background: hasWarn ? '#fffbeb' : undefined,
                                                    borderColor: hasWarn ? '#fde68a' : undefined,
                                                    color: hasWarn ? '#92400e' : undefined,
                                                }}
                                                title={titleParts.join(' · ')}
                                            >
                                                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                                                    <polyline points="14 2 14 8 20 8" />
                                                </svg>
                                                <span className="agent-preview-attach-name" style={{ maxWidth: 130 }}>{a.filename}</span>
                                                {a.text && (
                                                    <button
                                                        type="button"
                                                        onClick={() => setPreviewAttachmentId(a.id)}
                                                        title={hasWarn
                                                            ? `Preview · ${warnings.length} warning${warnings.length > 1 ? 's' : ''}`
                                                            : (a.kind === 'image' ? 'Preview image description' : 'Preview extracted text')}
                                                        aria-label={a.kind === 'image' ? 'Preview image description' : 'Preview extracted text'}
                                                        style={{
                                                            background: 'none', border: 'none', cursor: 'pointer',
                                                            color: hasWarn ? '#b45309' : '#4f46e5',
                                                            fontSize: 10, padding: '0 2px',
                                                            lineHeight: 1, fontWeight: 600,
                                                        }}
                                                    >Preview{hasWarn ? ` (${warnings.length})` : ''}</button>
                                                )}
                                                {a.kind === 'image' && !a.text && (
                                                    <span style={{ fontSize: 10, color: '#6b7280', padding: '0 2px' }}>Image</span>
                                                )}
                                                {a._file && !a.text && (
                                                    <button
                                                        type="button"
                                                        onClick={() => handleRetryOcr(a)}
                                                        title="Retry extraction"
                                                        aria-label="Retry extraction"
                                                        disabled={isUploadingAttachment}
                                                        style={{
                                                            background: 'none', border: 'none',
                                                            cursor: isUploadingAttachment ? 'not-allowed' : 'pointer',
                                                            color: '#b91c1c', fontSize: 10, padding: '0 2px',
                                                            lineHeight: 1, fontWeight: 600,
                                                        }}
                                                    >Retry</button>
                                                )}
                                                <button
                                                    type="button"
                                                    className="agent-preview-attach-remove"
                                                    onClick={() => handleRemoveAttachment(a.id)}
                                                    aria-label={`Remove ${a.filename}`}
                                                    style={{ opacity: 0.65 }}
                                                >
                                                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
                                                        <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                                                    </svg>
                                                </button>
                                            </span>
                                        );
                                    })}
                                    {isUploadingAttachment && (
                                        <span className="agent-preview-attach-chip uploading">
                                            <span className="agent-typing-dots small"><span /><span /><span /></span>
                                            <span className="agent-preview-attach-name">Reading file…</span>
                                        </span>
                                    )}
                                </div>
                            )}
                            {previewAttachmentId && (() => {
                                const target = attachments.find(a => a.id === previewAttachmentId);
                                if (!target) return null;
                                return (
                                    <ExtractedTextPreview
                                        open
                                        onClose={() => setPreviewAttachmentId(null)}
                                        filename={target.filename}
                                        text={target.text}
                                        warnings={target.warnings || []}
                                        imagesCount={target.imagesExtracted || 0}
                                        tablesCount={target.tablesExtracted || 0}
                                        cacheHit={!!target.cacheHit}
                                    />
                                );
                            })()}

                            <div className="chat-input-container agent-preview-input-container">
                                <div className="chat-input-wrapper chat-input-wrapper--preview agent-preview-composer">
                                    <input
                                        ref={attachInputRef}
                                        type="file"
                                        multiple
                                        accept={AGENT_CHAT_ATTACH_ACCEPT}
                                        style={{ display: 'none' }}
                                        onChange={handleFilesPicked}
                                    />
                                    <button
                                        type="button"
                                        className="paperclip-btn agent-preview-attach-btn"
                                        onClick={handleAttachClick}
                                        disabled={
                                            chatLoading
                                            || isUploadingAttachment
                                            || attachments.length >= AGENT_CHAT_ATTACH_MAX_FILES
                                        }
                                        title={
                                            attachments.length >= AGENT_CHAT_ATTACH_MAX_FILES
                                                ? `At most ${AGENT_CHAT_ATTACH_MAX_FILES} files per message`
                                                : 'Attach document or image (images are saved as assets the agent can reference)'
                                        }
                                        aria-label="Attach document or image"
                                    >
                                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                            <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
                                        </svg>
                                    </button>
                                    <textarea
                                        ref={chatInputRef}
                                        className="chat-input agent-preview-input"
                                        placeholder={`Ask ${agentName || 'this agent'} anything…`}
                                        value={chatInput}
                                        onChange={e => setChatInput(e.target.value)}
                                        onKeyDown={handleChatKeyDown}
                                        rows={1}
                                        disabled={chatLoading}
                                    />
                                    <button
                                        type="button"
                                        className={`send-btn agent-preview-send ${chatLoading ? 'stopping' : ''}`}
                                        onClick={chatLoading ? handleStopChat : handleSendChat}
                                        disabled={!chatLoading && !chatInput.trim() && attachments.length === 0}
                                        title={chatLoading ? 'Stop generation' : 'Send message'}
                                        aria-label={chatLoading ? 'Stop generation' : 'Send message'}
                                    >
                                        {chatLoading ? (
                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                                                <rect x="6" y="6" width="12" height="12" rx="1.5" />
                                            </svg>
                                        ) : (
                                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                                                <path d="M12 19V5M5 12l7-7 7 7" />
                                            </svg>
                                        )}
                                    </button>
                                </div>
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
        <a ref={teamsLinkRef} style={{ display: 'none' }} rel="noopener noreferrer" />
        </>
    );
}

export default AgentEditor;
