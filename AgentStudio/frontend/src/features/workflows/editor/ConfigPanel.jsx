// SPDX-License-Identifier: MIT
import { useEffect, useRef, useState, Fragment } from 'react';
import { API_BASE, apiFetch } from '../../../config/api';
import useWorkflowStore from '../../../store/workflowStore';
import useAgentsStore from '../../../store/agentsStore';
import useDashboardStore from '../../../store/dashboardStore';
import { validateEntityName } from '../../../utils/validateName';
import CatalogPicker from '../../../components/common/CatalogPicker';
import KnowledgeSection, { KB_MODE_NONE, KB_MODE_EXISTING, KB_MODE_ADD } from '../../../components/common/KnowledgeSection';
import SampleDocSection from '../../../components/common/SampleDocSection';
import CommonConfirmModal from '../../../components/common/ConfirmModal';
import GenerateInstructionsModal from '../../../components/common/GenerateInstructionsModal';
import TriggerSection from '../../triggers/TriggerSection';
import useAvailableModels, { MODEL_STATUS } from '../../../hooks/useAvailableModels';
import { LEGACY_NODE_MODEL } from '../../../config/models';
import useCurrentUser from '../../../hooks/useCurrentUser';
import { stripProviderPrefix } from '../../../utils/modelLabel';
import { getMaxTokensForModel } from '../../../utils/modelMaxTokens';
import ConditionBuilder from './conditions/ConditionBuilder';
import LoopWhileEditor from './conditions/LoopWhileEditor';
import SubflowPicker from './SubflowPicker';
import LoopItemsPicker from './LoopItemsPicker';
import { getUpstreamNodeId } from './helpers/loopPicker';

const HITL_MODE_LABELS = {
    off:            'Disabled',
    before_tool:    'Approve tool calls',
    after_response: 'Approve final response',
    both:           'Approve tool calls and final response',
};

function ConfigPanel() {
    const [showDeleteModal, setShowDeleteModal] = useState(false);
    const [showGenerateModal, setShowGenerateModal] = useState(false);
    const [showModelParameters, setShowModelParameters] = useState(false);
    const [showCatalogTools, setShowCatalogTools] = useState(false);
    const [showKnowledge, setShowKnowledge] = useState(false);
    const [showSampleDoc, setShowSampleDoc] = useState(false);
    const [showHitl, setShowHitl] = useState(false);
    const selectedNodeId = useWorkflowStore((state) => state.selectedNodeId);
    const nodes = useWorkflowStore((state) => state.nodes);
    const updateNodeData = useWorkflowStore((state) => state.updateNodeData);
    const removeNode = useWorkflowStore((state) => state.removeNode);
    const workflowId = useWorkflowStore((state) => state.workflowId);
    // Loop picker subscribes to the *derived* upstream node id (a string|null)
    // so it re-renders only when the edge wiring into the selected Loop node
    // actually changes — not on every unrelated edge edit.
    const loopUpstreamId = useWorkflowStore(
        (state) => getUpstreamNodeId(state.edges, selectedNodeId),
    );
    const activeThreadId = useWorkflowStore((state) => state.activeThreadId);
    const workflowKnowledge = useWorkflowStore((state) => state.workflowKnowledge);
    // Reactive subscription so the in-canvas Agent Name validator updates as
    // the agents catalog loads or changes elsewhere in the app.
    const savedAgents = useAgentsStore((state) => state.agents);
    const loadSavedAgents = useAgentsStore((state) => state.loadAgents);
    // Reactive subscription so the in-canvas Agent Name validator picks up
    // freshly-saved workflows too — names must be unique across both catalogs.
    const savedWorkflows = useDashboardStore((state) => state.workflows);
    const loadSavedWorkflows = useDashboardStore((state) => state.loadWorkflows);

    useEffect(() => {
        if (!savedAgents    || savedAgents.length    === 0) loadSavedAgents();
        if (!savedWorkflows || savedWorkflows.length === 0) loadSavedWorkflows();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    // The store keeps a temporary session id until the workflow has been
    // persisted (id starts with "workflow-" after save). Only show triggers
    // when the workflow has a backend id — otherwise creating a trigger
    // would fail with "Workflow not found".
    const workflowSaved = typeof workflowId === 'string' && workflowId.startsWith('workflow-');

    // Feature flag: show/hide the per-node trigger section.
    // Reads ABSTUDIO_AGENT_TRIGGERS_ENABLED from the root .env via the
    // existing /triggers/config endpoint — no new files or registrations needed.
    const [agentTriggersEnabled, setAgentTriggersEnabled] = useState(false);
    useEffect(() => {
        apiFetch('/triggers/config')
            .then(d => setAgentTriggersEnabled(!!d?.agent_triggers_enabled))
            .catch(() => {});
    }, []);

    const selectedNode = nodes.find((n) => n.id === selectedNodeId);
    const currentModelName = selectedNode?.data?.modelName || '';
    const {
        models: availableModels,
        providers: availableProviders,
        defaultModel,
        provider: backendProvider,
        status: modelsStatus,
        error: modelsError,
    } = useAvailableModels(currentModelName);

    const currentUser = useCurrentUser();

    // Normalize backend shapes into a single {label, options:[{id,label}]}
    // structure so the Model and Judge-model dropdowns share one render
    // path. Mirrors AgentEditor.jsx — when the backend returns grouped
    // providers (Anthropic / OpenAI / Local (In-house)) we render real
    // <optgroup>s with the full "Claude Sonnet 4.6 (claude-sonnet-4-6)"
    // labels; otherwise we synthesize a single flat group from the legacy
    // flat ID list.
    const buildModelOptionGroups = (fallbackId) => {
        if (availableProviders && availableProviders.length > 0) {
            return availableProviders.map((g) => ({
                label: g.provider,
                options: (g.models || []).map((m) => ({
                    id: m.id,
                    label: m.label || stripProviderPrefix(m.id),
                })),
            }));
        }
        const flat = availableModels.length
            ? availableModels
            : [fallbackId || defaultModel || ''];
        const options = flat
            .filter(Boolean)
            .map((id) => ({ id, label: stripProviderPrefix(id) }));
        return options.length ? [{ label: null, options }] : [];
    };
    const renderModelOptions = (groups) =>
        groups.map((group, i) => {
            const options = group.options.map((o) => (
                <option key={o.id} value={o.id}>{o.label}</option>
            ));
            return group.label
                ? <optgroup key={group.label} label={group.label}>{options}</optgroup>
                : <Fragment key={`flat-${i}`}>{options}</Fragment>;
        });

    // Per-node lock: once the user explicitly picks a model for a given
    // agent node, this effect must not overwrite that pick even if a
    // later catalogue refresh momentarily reports the model as missing
    // (race that previously snapped every selection back to ``defaultModel``).
    const userPickedModelNodesRef = useRef(new Set());
    useEffect(() => {
        // Re-derive selectedNode inside the effect to avoid using the unstable
        // object reference (nodes.find returns a new object every render) as a
        // dependency, which would cause an infinite update loop.
        const node = nodes.find((n) => n.id === selectedNodeId);
        if (node?.type !== 'agent') return;
        // User already chose a model for this node — leave it alone.
        if (userPickedModelNodesRef.current.has(node.id)) return;
        const currentModel = node.data?.modelName || '';
        const legacyWorkflowDefault =
            currentModel === LEGACY_NODE_MODEL &&
            node.data?.provider === 'google' &&
            (node.data?.maxTokens == null || node.data?.maxTokens === 2048);
        // Honor an existing non-blank model on the node (saved workflow,
        // copy-paste, etc.) even if the catalogue hasn't surfaced it yet.
        // The old workflow-node default is not a user pick; if the current
        // catalogue does not contain it, replace it with the displayed default.
        const hasExplicit = !!currentModel.trim() && !legacyWorkflowDefault;
        const nextModel = hasExplicit
            ? currentModel
            : (availableModels.includes(currentModel) ? currentModel : defaultModel);
        const nextMaxTokens = getMaxTokensForModel(nextModel);
        const modelChanged = node.data?.modelName !== nextModel;
        const needsEnvBackedConfig =
            node.data?.provider !== backendProvider ||
            node.data?.apiKey ||
            node.data?.baseUrl ||
            modelChanged;

        if (needsEnvBackedConfig && nextModel) {
            updateNodeData(node.id, {
                provider: backendProvider,
                modelName: nextModel,
                ...(modelChanged ? { maxTokens: nextMaxTokens } : {}),
                apiKey: '',
                baseUrl: '',
            });
        }
    }, [availableModels, defaultModel, backendProvider, selectedNodeId, nodes, updateNodeData]);

    if (!selectedNode) {
        return (
            <div className="config-panel animate-slide-in-right">
                <h3 className="config-panel-title">Configuration</h3>
                <div className="config-empty">
                    <div className="config-empty-icon">
                        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" opacity="0.5">
                            <circle cx="12" cy="12" r="3" />
                            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
                        </svg>
                    </div>
                    <p>Select a node to configure</p>
                </div>
            </div>
        );
    }

    const handleDeleteClick = () => {
        setShowDeleteModal(true);
    };

    const handleDeleteConfirm = () => {
        removeNode(selectedNodeId);
        setShowDeleteModal(false);
    };

    const handleDeleteCancel = () => {
        setShowDeleteModal(false);
    };

    const acceptGeneratedInstructions = (text) => {
        updateNodeData(selectedNodeId, { instructions: text });
    };

    const handleChange = (field, value) => {
        updateNodeData(selectedNodeId, { [field]: value });
    };

    if (selectedNode.type === 'start' || selectedNode.type === 'end') {
        return (
            <div className="config-panel animate-slide-in-right">
                <CommonConfirmModal
                    isOpen={showDeleteModal}
                    title="Delete Node"
                    message={`Are you sure you want to delete the "${selectedNode.type}" node?`}
                    onConfirm={handleDeleteConfirm}
                    onCancel={handleDeleteCancel}
                />
                <div className="config-header">
                    <h3 className="config-panel-title">
                        {selectedNode.type === 'start' ? 'Start Node' : 'End Node'}
                    </h3>
                    {selectedNode.type === 'end' && (
                        <button className="delete-icon-btn" onClick={handleDeleteClick} title="Delete Node">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                <polyline points="3 6 5 6 21 6" />
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                            </svg>
                        </button>
                    )}
                </div>
                <div className="config-empty">
                    <div className="config-empty-icon">
                        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" opacity="0.5">
                            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
                            <polyline points="22 4 12 14.01 9 11.01" />
                        </svg>
                    </div>
                    <p>No configuration needed</p>
                </div>
            </div>
        );
    }

    // Sub-flow node configuration — links the canvas node to a saved agent or
    // workflow. Same shape as `getDefaultNodeData('subflow')`.
    if (selectedNode.type === 'subflow') {
        const subData = selectedNode.data || {};
        const pickerValue = {
            kind: subData.kind || 'agent',
            refId: subData.refId || '',
            refName: subData.refName || '',
        };

        return (
            <div className="config-panel animate-slide-in-right">
                <CommonConfirmModal
                    isOpen={showDeleteModal}
                    title="Delete Node"
                    message="Remove this Existing Asset reference from the workflow?"
                    onConfirm={handleDeleteConfirm}
                    onCancel={handleDeleteCancel}
                />
                <div className="config-header">
                    <h3 className="config-panel-title">Existing Workflow / Agent</h3>
                    <button className="delete-icon-btn" onClick={handleDeleteClick} title="Delete Node">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <polyline points="3 6 5 6 21 6" />
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                        </svg>
                    </button>
                </div>
                <div className="config-form">
                    <div className="form-group">
                        <label className="form-label">Link to</label>
                        <SubflowPicker
                            mode="single"
                            value={pickerValue}
                            excludeWorkflowId={workflowId}
                            onChange={(next) => updateNodeData(selectedNodeId, {
                                kind: next.kind,
                                refId: next.refId,
                                refName: next.refName,
                            })}
                        />
                        <span className="form-hint">
                            The previous node&apos;s output flows in as the input to this
                            existing {pickerValue.kind || 'asset'}. Its final response flows out
                            to the next node — so you can chain it after a classifier or behind
                            a condition branch.
                        </span>
                    </div>
                </div>
            </div>
        );
    }

    // Loop node — schema mirrors `getDefaultNodeData('loop')` in
    // workflowStore.js and drives `_run_loop` in backend/native_engine.py.
    // Keep the option `value=` strings in lockstep with backend
    // engine/interface.py:LOOP_MODES.
    if (selectedNode.type === 'loop') {
        const loopData = selectedNode.data || {};
        const loopMode = (loopData.mode || 'for_each').toLowerCase();
        const loopUpstreamNode = loopUpstreamId
            ? nodes.find((n) => n.id === loopUpstreamId)
            : null;

        return (
            <div className="config-panel animate-slide-in-right">
                <CommonConfirmModal
                    isOpen={showDeleteModal}
                    title="Delete Node"
                    message="Are you sure you want to delete this Loop node?"
                    onConfirm={handleDeleteConfirm}
                    onCancel={handleDeleteCancel}
                />
                <div className="config-header">
                    <h3 className="config-panel-title">Loop Configuration</h3>
                    <button className="delete-icon-btn" onClick={handleDeleteClick} title="Delete Node">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <polyline points="3 6 5 6 21 6" />
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                        </svg>
                    </button>
                </div>
                <div className="config-form">
                    {/* Loop display name — optional identifier surfaced on
                        the canvas and in run timelines. Global concept so
                        it lives above the mode selector, not inside the
                        advanced evaluator block. */}
                    <div className="form-group">
                        <label className="form-label">Loop name</label>
                        <input
                            type="text"
                            className="form-input"
                            placeholder="Optional — used in run timelines"
                            value={loopData.name || ''}
                            onChange={(e) => handleChange('name', e.target.value)}
                        />
                    </div>

                    <div className="form-group">
                        <label className="form-label">Loop Mode</label>
                        {/* Display order is curated for discoverability —
                            most common / general-purpose mode first, list
                            iteration last. Backend `mode` values stay the
                            same so saved workflows are untouched:
                              1. While   (most common — condition-driven)
                              2. Count   (deterministic — fixed N)
                              3. For-each (specialised — list iteration) */}
                        <select
                            className="form-select"
                            value={loopMode}
                            onChange={(e) => handleChange('mode', e.target.value)}
                        >
                            <option value="while">Continue while a condition is true</option>
                            <option value="count">Run a fixed number of times</option>
                            <option value="for_each">Run for every item in a list</option>
                        </select>
                        <span className="form-hint">
                            The body of the loop runs once per iteration. Connect the body
                            handle back to this Loop node so iterations can restart, and
                            connect the exit handle to whatever comes next.
                        </span>
                    </div>

                    {loopMode === 'for_each' && (
                        <div className="form-group">
                            <label className="form-label">List to iterate</label>
                            <LoopItemsPicker
                                value={loopData.itemsExpression || 'input.items'}
                                onChange={(next) => handleChange('itemsExpression', next)}
                                upstreamNodeId={loopUpstreamId}
                                upstreamNodeName={loopUpstreamNode?.data?.name || loopUpstreamNode?.type || ''}
                                threadId={activeThreadId}
                            />
                            <div className="loop-picker-reference">
                                Inside the loop, reference the current item as
                                <code> {`{{loop.item}}`}</code> and the iteration number as
                                <code> {`{{loop.index}}`}</code>.
                            </div>
                        </div>
                    )}

                    {loopMode === 'count' && (
                        <div className="form-group">
                            <label className="form-label">Number of iterations</label>
                            <input
                                type="number"
                                className="form-input"
                                min="1"
                                value={loopData.count ?? 3}
                                onChange={(e) => handleChange('count', parseInt(e.target.value, 10) || 0)}
                            />
                            <span className="form-hint">
                                The body of the loop runs exactly this many times.
                            </span>
                        </div>
                    )}

                    {/* `while` mode — single flat condition editor (no
                        routing-cases, no ELSE branch, no Simple/Advanced
                        toggle). A loop has one continuation predicate, so
                        the UI matches the original Loop screenshot:
                        "Continue while" header + field/operator/value rows
                        + "Add condition". Persisted as a single-element
                        `cases` array so the backend `_run_loop` keeps
                        consuming it via build_expression_from_case
                        unchanged. */}
                    {loopMode === 'while' && (
                        <div className="form-group">
                            <LoopWhileEditor
                                cases={loopData.cases || []}
                                onChange={(newCases) => handleChange('cases', newCases)}
                            />
                            <span className="form-hint">
                                The loop continues while this expression is true.
                                When it evaluates false (or fails to evaluate),
                                the loop exits through the bottom handle.
                            </span>
                        </div>
                    )}

                    {/* Outside the mode-specific blocks: applies in every mode as a
                        runaway safety ceiling. The engine treats it as a hard cap. */}
                    <div className="form-group">
                        <label className="form-label">Maximum iterations</label>
                        <input
                            type="number"
                            className="form-input"
                            min="1"
                            value={loopData.maxIterations ?? 25}
                            onChange={(e) => handleChange('maxIterations', parseInt(e.target.value, 10) || 1)}
                        />
                        <span className="form-hint">
                            Safety ceiling. The loop stops after this many iterations no
                            matter which mode it is using.
                        </span>
                    </div>

                    {/* ----------------------------------------------------------
                        AI Evaluator (advanced) — opt-in LLM-as-judge with hybrid
                        stop policy. Wired through to
                        backend/app/engine/loop_evaluator.py via the keys the
                        serializer at workflowStore.js::getWorkflowForExecution
                        forwards. Collapsed by default so the basic Loop config
                        stays uncluttered. Only relevant for refinement loops
                        ('while' / 'count' modes); for_each loops have a
                        deterministic stop condition and don't benefit from a
                        confidence-based judge, so we hide the whole section
                        there to avoid confusing the user.
                       ---------------------------------------------------------- */}
                    {loopMode !== 'for_each' && (
                        <details
                            className="form-group loop-evaluator-section"
                            open={!!loopData.useLlmEvaluator}
                        >
                            <summary className="loop-evaluator-summary">
                                AI Evaluator <span className="loop-evaluator-pill">advanced</span>
                            </summary>

                            {/* Master toggle. When off, every other control is
                                disabled — they keep their values so the user can
                                tune offline and flip the switch when ready. */}
                            <div className="form-group">
                                <label className="form-label loop-evaluator-toggle-label">
                                    <input
                                        type="checkbox"
                                        checked={!!loopData.useLlmEvaluator}
                                        onChange={(e) => handleChange('useLlmEvaluator', e.target.checked)}
                                    />
                                    Use LLM-as-judge for confidence scoring
                                </label>
                                <span className="form-hint">
                                    An independent LLM scores each iteration against a
                                    rubric (temperature 0). The score overrides the
                                    body agent's self-report — more reliable, lower
                                    hallucination. Costs one extra LLM call per round.
                                </span>
                            </div>

                            <fieldset
                                disabled={!loopData.useLlmEvaluator}
                                className="loop-evaluator-fields"
                            >
                                {/* Empty value falls back to the backend default
                                    model so existing saved workflows keep working. */}
                                <div className="form-group">
                                    <label className="form-label">Judge model</label>
                                    <select
                                        className="form-select"
                                        value={loopData.evaluatorModelName || defaultModel || ''}
                                        onChange={(e) => handleChange('evaluatorModelName', e.target.value)}
                                        disabled={!loopData.useLlmEvaluator || (modelsStatus === MODEL_STATUS.LOADING && availableModels.length === 0)}
                                    >
                                        {renderModelOptions(buildModelOptionGroups(loopData.evaluatorModelName))}
                                    </select>
                                    <span className="form-hint">
                                        {modelsStatus === MODEL_STATUS.LOADING
                                            ? 'Loading models...'
                                            : modelsStatus === MODEL_STATUS.ERROR
                                                ? `Using fallback model. ${modelsError}`
                                                : 'Pick the LLM that will score each iteration. Same list as agent nodes — backed by the AiNxt gateway.'}
                                    </span>
                                </div>

                                {/* Stop mode: fixed runs to max_iterations regardless
                                    of score (matches the "Run fixed N times" UX); adaptive
                                    honours the confidence + similarity + regression signals. */}
                                <div className="form-group">
                                    <label className="form-label">Stop policy</label>
                                    <select
                                        className="form-select"
                                        value={loopData.stopMode || 'adaptive'}
                                        onChange={(e) => handleChange('stopMode', e.target.value)}
                                    >
                                        <option value="adaptive">Adaptive — stop when good enough</option>
                                        <option value="fixed">Fixed — always run to max iterations</option>
                                    </select>
                                    <span className="form-hint">
                                        Adaptive exits early on high confidence, output
                                        convergence, or score regression. Fixed always
                                        runs every iteration but still returns the
                                        highest-scoring one.
                                    </span>
                                </div>

                                {/* Confidence threshold slider. Hidden in fixed mode
                                    because it has no effect there. */}
                                {(loopData.stopMode || 'adaptive') === 'adaptive' && (
                                    <>
                                        <div className="form-group">
                                            <label className="form-label">
                                                Confidence threshold:{' '}
                                                <span className="loop-evaluator-slider-value">
                                                    {Math.round(((loopData.confidenceThreshold ?? 0.85)) * 100)}%
                                                </span>
                                            </label>
                                            <input
                                                type="range"
                                                className="form-range"
                                                min="0.5"
                                                max="0.99"
                                                step="0.01"
                                                value={loopData.confidenceThreshold ?? 0.85}
                                                onChange={(e) => handleChange('confidenceThreshold', parseFloat(e.target.value))}
                                            />
                                            <span className="form-hint">
                                                Exit when the judge's score reaches this
                                                threshold. 85% is a good default for
                                                "polished but not over-cooked".
                                            </span>
                                        </div>

                                        <div className="form-group">
                                            <label className="form-label">
                                                Convergence similarity:{' '}
                                                <span className="loop-evaluator-slider-value">
                                                    {Math.round(((loopData.similarityThreshold ?? 0.95)) * 100)}%
                                                </span>
                                            </label>
                                            <input
                                                type="range"
                                                className="form-range"
                                                min="0.80"
                                                max="0.99"
                                                step="0.01"
                                                value={loopData.similarityThreshold ?? 0.95}
                                                onChange={(e) => handleChange('similarityThreshold', parseFloat(e.target.value))}
                                            />
                                            <span className="form-hint">
                                                Exit when the new output is this similar
                                                to the previous one — further iterations
                                                won't change the result.
                                            </span>
                                        </div>

                                        <div className="form-group">
                                            <label className="form-label">
                                                Regression delta:{' '}
                                                <span className="loop-evaluator-slider-value">
                                                    {((loopData.regressionDelta ?? 0.05)).toFixed(2)}
                                                </span>
                                            </label>
                                            <input
                                                type="range"
                                                className="form-range"
                                                min="0.01"
                                                max="0.20"
                                                step="0.01"
                                                value={loopData.regressionDelta ?? 0.05}
                                                onChange={(e) => handleChange('regressionDelta', parseFloat(e.target.value))}
                                            />
                                            <span className="form-hint">
                                                If a new iteration scores this much lower
                                                than the previous one, roll back to the
                                                best iteration so far. Guards against
                                                "polish-then-ruin".
                                            </span>
                                        </div>
                                    </>
                                )}

                                {/* Evaluator task — what "good" means for this loop.
                                    Empty falls back to a generic "Iteratively improve
                                    the artifact" instruction in the backend. */}
                                <div className="form-group">
                                    <label className="form-label">What the judge should look for</label>
                                    <textarea
                                        className="form-textarea"
                                        rows={3}
                                        placeholder="e.g. Build a comprehensive Deep Agents training plan for one week as a slide deck. Score for completeness and factual accuracy."
                                        value={loopData.evaluatorTask || ''}
                                        onChange={(e) => handleChange('evaluatorTask', e.target.value)}
                                    />
                                    <span className="form-hint">
                                        Describe the task the body agent is performing so
                                        the judge can grade against the right goal.
                                        Leave blank to use the loop's default.
                                    </span>
                                </div>

                                {/* Custom rubric override. When non-empty, fully
                                    replaces the judge's built-in system prompt — for
                                    domain experts who want total control. Most users
                                    won't touch this. */}
                                <div className="form-group">
                                    <label className="form-label">
                                        Custom rubric / judge prompt
                                        <span className="loop-evaluator-optional"> (optional)</span>
                                    </label>
                                    <textarea
                                        className="form-textarea"
                                        rows={4}
                                        placeholder="Leave blank to use the built-in factual_correctness / completeness / instruction_adherence / format_validity rubric."
                                        value={loopData.evaluatorRubric || ''}
                                        onChange={(e) => handleChange('evaluatorRubric', e.target.value)}
                                    />
                                    <span className="form-hint">
                                        Overrides the judge's entire system prompt.
                                        The judge must still emit the standard JSON
                                        contract. Only use if you need domain-specific
                                        scoring the built-in rubric can't express.
                                    </span>
                                </div>
                            </fieldset>
                        </details>
                    )}
                </div>
            </div>
        );
    }

    // Condition node configuration
    if (selectedNode.type === 'condition') {
        const condCases = selectedNode.data.cases || [];
        const handleCasesChange = (newCases) => updateNodeData(selectedNodeId, { cases: newCases });

        return (
            <div className="config-panel animate-slide-in-right">
                <CommonConfirmModal
                    isOpen={showDeleteModal}
                    title="Delete Node"
                    message="Are you sure you want to delete this Condition node?"
                    onConfirm={handleDeleteConfirm}
                    onCancel={handleDeleteCancel}
                />
                <div className="config-header">
                    <h3 className="config-panel-title">Condition Node</h3>
                    <button className="delete-icon-btn" onClick={handleDeleteClick} title="Delete Node">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <polyline points="3 6 5 6 21 6" />
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                        </svg>
                    </button>
                </div>
                <div className="config-form">
                    <ConditionBuilder
                        cases={condCases}
                        onChange={handleCasesChange}
                    />
                </div>
            </div>
        );
    }

    const { data } = selectedNode;

    // Global name uniqueness for the in-canvas Agent Name field:
    //   1. Other agent nodes in this same workflow.
    //   2. All saved agents on the dashboard.
    //   3. All saved workflows (so an inline agent can't shadow a workflow's
    //      name and confuse the subflow picker / engine lookup).
    // Ids are namespaced so the validator's id-based exclusion never
    // accidentally matches across kinds.
    const otherWorkflowAgentItems = nodes
        .filter((n) => n.type === 'agent' && n.id !== selectedNodeId)
        .map((n) => ({ id: n.id, name: (n.data && n.data.name) || '' }));
    const savedAgentItems = (savedAgents || []).map((a) => ({
        id: `saved-agent:${a.id}`, name: a.name,
    }));
    const savedWorkflowItems = (savedWorkflows || []).map((w) => ({
        id: `saved-wf:${w.id}`, name: w.name,
    }));
    const agentNameError = validateEntityName(data.name || '', 'agent', {
        existingItems: [...otherWorkflowAgentItems, ...savedAgentItems, ...savedWorkflowItems],
        currentId: selectedNodeId,
    });

    return (
        <div className="config-panel animate-slide-in-right">
            <CommonConfirmModal
                isOpen={showDeleteModal}
                title="Delete Node"
                message={`Are you sure you want to delete "${data.name || 'Agent'}" node? This action cannot be undone.`}
                onConfirm={handleDeleteConfirm}
                onCancel={handleDeleteCancel}
            />
            <GenerateInstructionsModal
                isOpen={showGenerateModal}
                onClose={() => setShowGenerateModal(false)}
                onAccept={acceptGeneratedInstructions}
            />
            <div className="config-header">
                <h3 className="config-panel-title">Agent Configuration</h3>
                <button className="delete-icon-btn" onClick={handleDeleteClick} title="Delete Node">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <polyline points="3 6 5 6 21 6" />
                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                    </svg>
                </button>
            </div>
            <div className="config-form">
                {/* Agent Name */}
                <div className="form-group">
                    <label className="form-label">Agent Name</label>
                    <input
                        type="text"
                        className={`form-input${agentNameError ? ' form-input-error' : ''}`}
                        value={data.name || ''}
                        onChange={(e) => handleChange('name', e.target.value)}
                        placeholder="e.g., Researcher"
                        aria-invalid={agentNameError ? 'true' : 'false'}
                    />
                    {agentNameError && (
                        <div
                            className="form-input-error-msg"
                            role="alert"
                            style={{ color: '#dc2626', fontSize: 12, marginTop: 4 }}
                        >
                            {agentNameError}
                        </div>
                    )}
                </div>

                {/* Instructions */}
                <div className="form-group">
                    <div className="form-label-row">
                        <label className="form-label">Instructions</label>
                        <button
                            className="generate-btn"
                            onClick={() => setShowGenerateModal(true)}
                            title="Generate with AI"
                        >
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" />
                            </svg>
                            Generate
                        </button>
                    </div>
                    <textarea
                        className="form-textarea"
                        value={data.instructions || ''}
                        onChange={(e) => handleChange('instructions', e.target.value)}
                        placeholder="Describe what this agent should do..."
                    />
                </div>

                <div className="form-divider">LLM Settings</div>

                <div className="form-group">
                    <label className="form-label">Model</label>
                    <select
                        className="form-select"
                        value={data.modelName || defaultModel || ''}
                        onChange={(e) => {
                            // Lock in the user's pick so the catalogue-sync
                            // effect above can't overwrite it for this node.
                            userPickedModelNodesRef.current.add(selectedNodeId);
                            // Auto-bump maxTokens to the new model's cap so
                            // users immediately see the full headroom. They
                            // can still slide it back down.
                            const modelCap = getMaxTokensForModel(e.target.value);
                            updateNodeData(selectedNodeId, {
                                modelName: e.target.value,
                                provider: backendProvider,
                                maxTokens: modelCap,
                                apiKey: '',
                                baseUrl: '',
                            });
                        }}
                        disabled={modelsStatus === MODEL_STATUS.LOADING && availableModels.length === 0}
                    >
                        {renderModelOptions(buildModelOptionGroups(data.modelName))}
                    </select>
                    <span className="form-hint">
                        {modelsStatus === MODEL_STATUS.LOADING
                            ? 'Loading models...'
                            : modelsStatus === MODEL_STATUS.ERROR
                                ? `Using fallback model. ${modelsError}`
                                : ''}
                    </span>
                </div>

                {/* Per-node subagent (swarm) opt-in. Tri-state pin:
                      enable_subagents=true   → force ON for this node,
                                                overrides run-level OFF
                                                (matches the UI hint
                                                "Per-node pins take
                                                precedence").
                      disable_subagents=true  → force OFF (legacy field
                                                still honoured by the
                                                backend for back-compat).
                      neither                 → inherit run-level toggle.

                    Default: OFF so the node-level toggle starts disabled
                    just like the chat-panel run-level toggle. When ON,
                    the backend always injects WorkflowSwarmTool +
                    SWARM_POLICY_ADDENDUM for this node regardless of
                    the run-level flag. See native_engine.py
                    `_disable_subagents` gate. */}
                <div className="form-group">
                    <div
                        className={`switch-row ${data.enable_subagents ? 'switch-row--on' : ''}`}
                    >
                        <div className="switch-row-text">
                            <span className="switch-row-title">
                                Use subagents (swarm) for this node
                            </span>
                            <span className="switch-row-hint">
                                Allow this agent to delegate complex
                                sub-tasks to specialised subagents at
                                run time. Overrides the chat-panel
                                run-level toggle for this node only.
                            </span>
                        </div>
                        <label
                            className="switch"
                            aria-label="Use subagents for this node"
                        >
                            <input
                                type="checkbox"
                                checked={!!data.enable_subagents}
                                onChange={(e) => {
                                    const checked = e.target.checked;
                                    handleChange('enable_subagents', checked);
                                    // Clear the legacy opt-OUT pin when
                                    // the user explicitly opts IN, so the
                                    // two flags never contradict each
                                    // other in the saved workflow JSON.
                                    if (checked && data.disable_subagents) {
                                        handleChange('disable_subagents', false);
                                    }
                                }}
                            />
                            <span className="switch-track">
                                <span className="switch-thumb" />
                            </span>
                        </label>
                    </div>
                </div>


                <div className={`config-collapse ${showModelParameters ? 'config-collapse--open' : ''}`}>
                    <button
                        type="button"
                        className="config-collapse-trigger"
                        onClick={() => setShowModelParameters((open) => !open)}
                        aria-expanded={showModelParameters}
                    >
                        <span className="config-collapse-title">Model Parameters</span>
                        <span className="config-collapse-summary">
                            T {(data.temperature || 0.7).toFixed(2)} | {data.maxTokens || 2048} tokens | P {(data.topP || 1.0).toFixed(2)}
                        </span>
                        <span className="config-collapse-chevron" aria-hidden="true">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M6 9l6 6 6-6" />
                            </svg>
                        </span>
                    </button>

                    {showModelParameters && (
                        <div className="config-collapse-body">
                            {/* Temperature */}
                            <div className="form-group slider-group">
                                <div className="slider-header">
                                    <label className="form-label">Temperature</label>
                                    <span className="slider-value">{(data.temperature || 0.7).toFixed(2)}</span>
                                </div>
                                <input
                                    type="range"
                                    className="form-slider"
                                    min="0"
                                    max="1"
                                    step="0.1"
                                    value={data.temperature || 0.7}
                                    onChange={(e) => handleChange('temperature', parseFloat(e.target.value))}
                                />
                            </div>

                            {/* Max Tokens — cap follows the selected model. */}
                            {(() => {
                                const modelCap = getMaxTokensForModel(data.modelName || defaultModel);
                                const currentMax = Math.min(data.maxTokens || 2048, modelCap);
                                return (
                                    <div className="form-group slider-group">
                                        <div className="slider-header">
                                            <label className="form-label">Max Tokens</label>
                                            <span className="slider-value">{currentMax} / {modelCap}</span>
                                        </div>
                                        <input
                                            type="range"
                                            className="form-slider"
                                            min="256"
                                            max={modelCap}
                                            step="256"
                                            value={currentMax}
                                            onChange={(e) => handleChange('maxTokens', parseInt(e.target.value))}
                                        />
                                    </div>
                                );
                            })()}

                            {/* Top P */}
                            <div className="form-group slider-group">
                                <div className="slider-header">
                                    <label className="form-label">Top P</label>
                                    <span className="slider-value">{(data.topP || 1.0).toFixed(2)}</span>
                                </div>
                                <input
                                    type="range"
                                    className="form-slider"
                                    min="0"
                                    max="1"
                                    step="0.1"
                                    value={data.topP || 1.0}
                                    onChange={(e) => handleChange('topP', parseFloat(e.target.value))}
                                />
                            </div>
                        </div>
                    )}
                </div>

                <div className={`config-collapse ${showCatalogTools ? 'config-collapse--open' : ''}`}>
                    <button
                        type="button"
                        className="config-collapse-trigger"
                        onClick={() => setShowCatalogTools((open) => !open)}
                        aria-expanded={showCatalogTools}
                    >
                        <span className="config-collapse-title">Catalog Tools & Skills</span>
                        <span className="config-collapse-summary">
                            {(data.tools || []).length} tools | {(data.skills || []).length} skills attached
                        </span>
                        <span className="config-collapse-chevron" aria-hidden="true">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M6 9l6 6 6-6" />
                            </svg>
                        </span>
                    </button>

                    {showCatalogTools && (
                        <div className="config-collapse-body">
                            <CatalogPicker
                                kind="tools"
                                attached={data.tools || []}
                                onChange={(next) => handleChange('tools', next)}
                            />
                            <CatalogPicker
                                kind="skills"
                                attached={data.skills || []}
                                onChange={(next) => handleChange('skills', next)}
                            />
                        </div>
                    )}
                </div>

                {/* Knowledge attachment — drives RAG retrieval at runtime. */}
                <div className={`config-collapse ${showKnowledge ? 'config-collapse--open' : ''}`}>
                    <button
                        type="button"
                        className="config-collapse-trigger"
                        onClick={() => setShowKnowledge((open) => !open)}
                        aria-expanded={showKnowledge}
                    >
                        <span className="config-collapse-title">Knowledge</span>
                        <span className="config-collapse-summary">
                            {(() => {
                                const k = data.knowledge || { mode: KB_MODE_NONE };
                                if (k.mode === KB_MODE_NONE) return 'None';
                                if (k.mode === KB_MODE_EXISTING) {
                                    // Prefer graph-model `scopes` when present;
                                    // fall back to legacy `namespaces` for
                                    // pre-migration agents.
                                    const s = (k.scopes || []).length;
                                    if (s > 0) return `Existing KB · ${s} scope${s === 1 ? '' : 's'}`;
                                    const n = (k.namespaces || []).length;
                                    return n === 0 ? 'Existing KB · all scopes' : `Existing KB · ${n} namespace${n === 1 ? '' : 's'}`;
                                }
                                if (k.mode === KB_MODE_ADD) {
                                    const docs = (k.uploaded_doc_ids || []).length;
                                    return `Add KB · ${docs} doc${docs === 1 ? '' : 's'} queued`;
                                }
                                return k.mode;
                            })()}
                        </span>
                        <span className="config-collapse-chevron" aria-hidden="true">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M6 9l6 6 6-6" />
                            </svg>
                        </span>
                    </button>

                    {showKnowledge && (
                        <div className="config-collapse-body">
                            {/* Surface workflow-level inheritance so users
                                aren't confused when RAG fires despite the
                                node showing "None". */}
                            {(() => {
                                const nodeKbMode = (data.knowledge && data.knowledge.mode) || KB_MODE_NONE;
                                const wfKbMode = (workflowKnowledge && workflowKnowledge.mode) || KB_MODE_NONE;
                                // Prefer graph-model scopes; fall back to
                                // legacy namespaces so pre-migration workflow
                                // configs still render a count.
                                const wfScopeCount = Array.isArray(workflowKnowledge && workflowKnowledge.scopes)
                                    ? workflowKnowledge.scopes.length
                                    : 0;
                                const wfNsCount = Array.isArray(workflowKnowledge && workflowKnowledge.namespaces)
                                    ? workflowKnowledge.namespaces.length
                                    : 0;
                                const wfKbCount = wfScopeCount > 0 ? wfScopeCount : wfNsCount;
                                if (nodeKbMode !== KB_MODE_NONE) return null;
                                if (wfKbMode === KB_MODE_NONE) return null;
                                return (
                                    <div style={{
                                        marginBottom: 10,
                                        padding: '8px 10px',
                                        background: '#eff6ff',
                                        border: '1px solid #bfdbfe',
                                        borderRadius: 6,
                                        fontSize: 11,
                                        color: '#1e40af',
                                    }}>
                                        <strong>Inheriting from workflow</strong>
                                        {' — '}
                                        this agent will retrieve from the workflow-level KB
                                        {wfKbCount > 0 ? ` (${wfKbCount} KB${wfKbCount === 1 ? '' : 's'} attached)` : ''}.
                                        Switch to <em>Existing KB</em> or <em>Add KB</em> to override.
                                    </div>
                                );
                            })()}
                            <KnowledgeSection
                                value={data.knowledge || { mode: KB_MODE_NONE }}
                                onChange={(next) => handleChange('knowledge', next)}
                                userDept={currentUser.department}
                                isApprover={currentUser.canApprove}
                                isAdmin={currentUser.role === 'admin'}
                            />
                        </div>
                    )}
                </div>

                {/* Sample document (look-and-feel reference). Optional per-node
                    slot the agent studies to mimic branding, fonts, headers,
                    slide layouts, etc. Metadata is stored inline on
                    ``node.data.sample_doc`` (round-tripped by the workflow
                    save path); the physical file lives under
                    ``<GENERATED_FILES_DIR>/workflow_samples/<workflow_id>/<node_id>/``
                    and is served by the workflow-node endpoints in
                    ``app/api/agent_sample.py``. Same UI component as the
                    standalone-agent editor for consistency. */}
                <div className={`config-collapse ${showSampleDoc ? 'config-collapse--open' : ''}`}>
                    <button
                        type="button"
                        className="config-collapse-trigger"
                        onClick={() => setShowSampleDoc((open) => !open)}
                        aria-expanded={showSampleDoc}
                    >
                        <span className="config-collapse-title">Sample document</span>
                        <span className="config-collapse-summary">
                            {(() => {
                                const sd = data.sample_doc || {};
                                if (!sd.path) return 'None';
                                return sd.name || `sample.${sd.kind}`;
                            })()}
                        </span>
                        <span className="config-collapse-chevron" aria-hidden="true">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M6 9l6 6 6-6" />
                            </svg>
                        </span>
                    </button>

                    {showSampleDoc && (
                        <div className="config-collapse-body">
                            <p style={{ fontSize: 11, color: '#6b7280', margin: '0 0 10px' }}>
                                Upload one document this node's outputs should resemble
                                (logos, fonts, headers, slide patterns). Optional — the
                                agent adapts structure and content freely.
                            </p>
                            <SampleDocSection
                                value={data.sample_doc || {}}
                                onChange={(next) => handleChange('sample_doc', next)}
                                endpoint={
                                    workflowSaved
                                        ? {
                                            upload: `${API_BASE}/agent-runner/workflows/${encodeURIComponent(workflowId)}/nodes/${encodeURIComponent(selectedNodeId)}/sample`,
                                            get: `${API_BASE}/agent-runner/workflows/${encodeURIComponent(workflowId)}/nodes/${encodeURIComponent(selectedNodeId)}/sample`,
                                            download: `${API_BASE}/agent-runner/workflows/${encodeURIComponent(workflowId)}/nodes/${encodeURIComponent(selectedNodeId)}/sample/download`,
                                            del: `${API_BASE}/agent-runner/workflows/${encodeURIComponent(workflowId)}/nodes/${encodeURIComponent(selectedNodeId)}/sample`,
                                        }
                                        : null
                                }
                                notReadyHint="Save the workflow first (give it a name), then attach a sample."
                            />
                        </div>
                    )}
                </div>

                {/* Human-in-the-Loop — pauses the agent at runtime so the user can
                    approve / reject tool calls or the final response. The backend
                    raises `hitl_interrupt` SSE events when mode != 'off'; the
                    ChatPanel renders a pause card and resumes the thread. */}
                <div className={`config-collapse ${showHitl ? 'config-collapse--open' : ''}`}>
                    <button
                        type="button"
                        className="config-collapse-trigger"
                        onClick={() => setShowHitl((open) => !open)}
                        aria-expanded={showHitl}
                    >
                        <span className="config-collapse-title">Human-in-the-Loop</span>
                        <span className="config-collapse-summary">
                            {HITL_MODE_LABELS[data.hitlMode || 'off'] || HITL_MODE_LABELS.off}
                        </span>
                        <span className="config-collapse-chevron" aria-hidden="true">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M6 9l6 6 6-6" />
                            </svg>
                        </span>
                    </button>

                    {showHitl && (
                        <div className="config-collapse-body">
                            <div className="form-group">
                                <label className="form-label">Mode</label>
                                <select
                                    className="form-select"
                                    value={data.hitlMode || 'off'}
                                    onChange={(e) => handleChange('hitlMode', e.target.value)}
                                >
                                    <option value="off">{HITL_MODE_LABELS.off}</option>
                                    <option value="before_tool">{HITL_MODE_LABELS.before_tool}</option>
                                    <option value="after_response">{HITL_MODE_LABELS.after_response}</option>
                                    <option value="both">{HITL_MODE_LABELS.both}</option>
                                </select>
                                <span className="form-hint">
                                    The agent pauses at the chosen step so you can approve, edit, or
                                    reject it in the chat panel before it continues.
                                </span>
                            </div>
                        </div>
                    )}
                </div>

                {agentTriggersEnabled && (
                    <TriggerSection
                        targetKind="workflow"
                        targetId={workflowSaved ? workflowId : null}
                        nodeId={selectedNodeId}
                        disabled={!workflowSaved ? 'Save the workflow first to set a trigger.' : ''}
                        variant="compact"
                    />
                )}
            </div>
        </div>
    );
}

export default ConfigPanel;
