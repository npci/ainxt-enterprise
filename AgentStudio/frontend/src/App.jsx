// SPDX-License-Identifier: MIT
import { ReactFlowProvider } from '@xyflow/react';
import { useState, useEffect, useCallback, useRef, Component } from 'react';
import Sidebar from './features/workflows/editor/Sidebar';
import Canvas from './features/workflows/editor/Canvas';
import ConfigPanel from './features/workflows/editor/ConfigPanel';
import ChatPanel from './features/workflows/editor/ChatPanel';
import TriggerNotifications from './features/triggers/TriggerNotifications';

// Feature modules — each section lives in its own folder for portability
import WorkflowsDashboard from './features/workflows';
import RunningWorkflowToast from './features/workflows/RunningWorkflowToast';
import { AgentsDashboard, AgentEditor } from './features/agents';
import SkillsDashboard from './features/skills';
import { SubmitApprovalButton, StatusBadge } from './features/governance';
import ToolsDashboard from './features/tools';

import useWorkflowStore from './store/workflowStore';
import useDashboardStore from './store/dashboardStore';
import useAgentsStore from './store/agentsStore';
import useTemplateAdminStore from './store/templateAdminStore';   // optional template editor — safe to delete
import { validateEntityName, suggestFreeName } from './utils/validateName';
import { KB_MODE_NONE } from './components/common/KnowledgeSection';
import {
  ensureUserNamespace,
  loadOpenEditor,
  saveOpenEditor,
  clearOpenEditor,
  hasStoredOpenEditor,
  loadSelectedNode,
  saveSelectedNode,
} from './utils/editorPersistence';

// Drop React Flow runtime/measurement fields the canvas writes onto nodes on
// mount (``measured`` is RF v12's {width,height}). They are not user content;
// leaving them in the persisted graph / dirty-check made merely opening a
// template instance look "changed", firing a spurious autosave and demoting it
// from Live to Submit-for-Approval. Mirrors the RF subset of _VOLATILE_KEYS in
// backend governance_client.py.
const sanitizeNodesForPersist = (ns) => (ns || []).map((n) => {
  // eslint-disable-next-line no-unused-vars
  const {
    measured, width, height, positionAbsolute,
    selected, dragging, zIndex, handleBounds, internals, __rf,
    ...rest
  } = n;
  return rest;
});

// Single source of truth for the autosave snapshot string. Every dirty-check
// and lastSavedRef seed must go through this so key order + node sanitization
// never drift (drift re-introduces the spurious open-time save).
const buildSnapshot = (name, nodes, edges, knowledge) => JSON.stringify({
  name, nodes: sanitizeNodesForPersist(nodes), edges,
  knowledge: knowledge || { mode: KB_MODE_NONE },
});

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error('[ErrorBoundary] Caught error:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div data-ac="" style={{
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
          height: '100%', background: '#f8fafc', color: '#0f172a', fontFamily: 'var(--font-family-base, system-ui)', gap: '16px',
        }}>
          <div style={{ fontSize: '48px' }}>⚠️</div>
          <h2 style={{ margin: 0, color: '#ef4444' }}>Something went wrong</h2>
          <p style={{ margin: 0, color: '#64748b', maxWidth: '500px', textAlign: 'center' }}>
            {this.state.error?.message || 'An unexpected error occurred'}
          </p>
          <button
            onClick={() => this.setState({ hasError: false, error: null })}
            style={{
              marginTop: '8px', padding: '10px 24px', background: '#4f46e5', color: 'white',
              border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '14px',
            }}
          >
            Try Again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

// Chat preview panel sizing. The minimum keeps the chat usable; the
// maximum prevents it from crowding the canvas (where the workflow name
// and mode toggle live in the top-left).
const CHAT_WIDTH_DEFAULT = 480;
const CHAT_WIDTH_MIN = 340;
const CHAT_WIDTH_MAX = 720;

const NAV_ITEMS = [
  {
    id: 'workflows',
    label: 'Workflows',
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <path d="M17.5 14v3m0 3v.01M17.5 17h.01" />
        <path d="M10 6.5h4M17.5 10v4" />
      </svg>
    ),
  },
  {
    id: 'agents',
    label: 'Agents',
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <circle cx="12" cy="8" r="4" />
        <path d="M4 20c0-4 3.58-7 8-7s8 3 8 7" />
      </svg>
    ),
  },
  {
    id: 'skills',
    label: 'Skills',
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
      </svg>
    ),
  },
  {
    id: 'tools',
    label: 'Tools',
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
      </svg>
    ),
  },
];

function AppTopBar({ section, onSectionChange }) {
  return (
    <header className="app-topbar" aria-label="Main navigation">
      <div className="topbar-brand">
        <span className="topbar-wordmark">Build Studio</span>
      </div>

      <nav className="topbar-tabs" role="tablist" aria-label="Sections">
        {NAV_ITEMS.map(item => (
          <button
            key={item.id}
            type="button"
            role="tab"
            className={`topbar-tab ${section === item.id ? 'active' : ''}`}
            onClick={() => onSectionChange(item.id)}
            title={item.label}
            aria-selected={section === item.id}
          >
            <span className="topbar-tab-icon">{item.icon}</span>
            <span className="topbar-tab-label">{item.label}</span>
          </button>
        ))}
      </nav>

      {/* Anchors the bell to the topbar's right edge so it aligns with the
          tabs vertically, instead of floating over the viewport. */}
      <div className="topbar-end">
        <TriggerNotifications />
      </div>
    </header>
  );
}

// Remembers which dashboard tab (workflows / agents / skills / tools) the
// user last had open so a reload lands them back on it. This is pure UI
// chrome — the DB remains the source of truth for the actual entities — so
// localStorage is the right place for it (same rationale as the sidebar
// Chat/Buddy, which only use localStorage for view state, never data).
const SECTION_STORAGE_KEY = 'abstudio.activeSection';
const VALID_SECTIONS = ['workflows', 'agents', 'skills', 'tools'];

function loadStoredSection() {
  try {
    const saved = localStorage.getItem(SECTION_STORAGE_KEY);
    return VALID_SECTIONS.includes(saved) ? saved : 'workflows';
  } catch {
    return 'workflows';
  }
}

function App() {
  const [view, setView] = useState('dashboard');
  const [section, setSection] = useState(loadStoredSection);
  const [mode, setMode] = useState('edit');
  const [currentWorkflowId, setCurrentWorkflowId] = useState(null);
  // When set, the canvas is editing a template instead of a workflow —
  // `saveCurrentWorkflow` routes its PUT to /templates/{id} instead of
  // /workflows/{id}. Optional template editor feature; safe to remove.
  const [editingTemplateId, setEditingTemplateId] = useState(null);
  // When set, the editor is showing a chat-only PREVIEW of a template (no
  // clone/persist). Neither `currentWorkflowId` nor `editingTemplateId` is set
  // in this state, so autosave stays inert. Clicking the header "Edit" toggle
  // clones the template into a real workflow (see handleEditFromTemplatePreview).
  const [previewingTemplate, setPreviewingTemplate] = useState(null);
  const [workflowName, setWorkflowName] = useState('New workflow');
  const [isEditingName, setIsEditingName] = useState(false);
  // Inline name validation error shown under the workflow title in the
  // editor header. While set, autosave is suppressed (see saveCurrentWorkflow).
  const [workflowNameError, setWorkflowNameError] = useState(null);
  const [editingAgent, setEditingAgent] = useState(null);
  const [agentEditorMode, setAgentEditorMode] = useState('edit');
  // When set, the agent editor is showing a chat-only PREVIEW of an agent
  // template (no clone/persist). ``editingAgent`` stays null in this state,
  // so autosave stays inert and the Deploy button / StatusBadge stay hidden.
  // Clicking the header "Edit" toggle clones the template into a real agent
  // (see handlePromoteAgentTemplate).
  const [previewingAgentTemplate, setPreviewingAgentTemplate] = useState(null);
  // True on first load while the async open-editor restore resolves — only when
  // there's actually a pointer to restore, so a normal cold start (no stored
  // editor) shows the dashboard immediately with no loading flash. Prevents the
  // reverse flash: dashboard briefly showing before the editor swaps in.
  const [restoring, setRestoring] = useState(hasStoredOpenEditor);

  // Persist the active dashboard tab so a reload restores it.
  useEffect(() => {
    try { localStorage.setItem(SECTION_STORAGE_KEY, section); } catch { /* storage unavailable */ }
  }, [section]);

  // Guards the one-shot mount restore against React StrictMode's double-invoke
  // and against the persist effect firing before restore has run.
  const editorRestoredRef = useRef(false);

  // Persist which editor is open so a reload can reopen it. Only DB-addressable
  // editors are stored: a saved workflow (currentWorkflowId) or a saved agent.
  // Template-edit and scratch (unsaved) sessions have ids that aren't in the
  // dashboard list, so we clear the pointer rather than store an un-restorable
  // one. Skipped until the mount restore has completed so we don't overwrite
  // the stored pointer with the transient dashboard state during hydration.
  useEffect(() => {
    if (!editorRestoredRef.current) return;
    if (view === 'editor' && currentWorkflowId && !editingTemplateId) {
      saveOpenEditor({ kind: 'workflow', id: currentWorkflowId, mode });
    } else if (editingAgent?.id) {
      saveOpenEditor({ kind: 'agent', id: editingAgent.id, mode: agentEditorMode });
    } else {
      clearOpenEditor();
    }
  }, [view, currentWorkflowId, editingTemplateId, editingAgent, mode, agentEditorMode]);

  // Save indicator state
  const [saveStatus, setSaveStatus] = useState('saved'); // 'saved' | 'saving' | 'unsaved'
  const lastSavedRef = useRef(null);
  const workflowUpdatedAtRef = useRef(null);

  const [chatWidth, setChatWidth] = useState(CHAT_WIDTH_DEFAULT);
  const isResizing = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(0);

  const handleResizeMouseDown = useCallback((e) => {
    isResizing.current = true;
    startX.current = e.clientX;
    startWidth.current = chatWidth;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMouseMove = (e) => {
      if (!isResizing.current) return;
      const delta = startX.current - e.clientX;
      const newWidth = Math.min(CHAT_WIDTH_MAX, Math.max(CHAT_WIDTH_MIN, startWidth.current + delta));
      setChatWidth(newWidth);
    };

    const onMouseUp = () => {
      isResizing.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    };

    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
  }, [chatWidth]);

  const {
    nodes,
    edges,
    setNodes,
    setEdges,
    resetWorkflow,
    selectedNodeId,
    setSelectedNode,
    setWorkflowName: setStoreWorkflowName,
    setWorkflowId: setStoreWorkflowId,
    workflowKnowledge,
    setWorkflowKnowledge,
  } = useWorkflowStore();
  const isExecuting = useWorkflowStore((s) => s.isExecuting);
  // A run paused for human approval clears `isExecuting` (the composer and
  // "running" indicator must stop) but the run is very much still alive — it
  // is waiting on the approval card. Track the pause separately so navigating
  // to the dashboard mid-approval does not unmount the editor and abort it.
  const chatHitlRequest = useWorkflowStore((s) => s.chatHitlRequest);
  const { updateWorkflow, workflows: existingWorkflows, loadWorkflows, useTemplate } = useDashboardStore();
  const { loadAgents, agents: existingAgents } = useAgentsStore();

  // Remember which node's config panel is open per workflow so a reload
  // restores it. Gated on restore completion so hydration doesn't clobber it.
  useEffect(() => {
    if (!editorRestoredRef.current) return;
    if (view === 'editor' && currentWorkflowId && !editingTemplateId) {
      saveSelectedNode(currentWorkflowId, selectedNodeId);
    }
  }, [view, currentWorkflowId, editingTemplateId, selectedNodeId]);

  // Snapshot the workflow name as-loaded so the uniqueness check ignores the
  // row we're currently editing.
  const initialWorkflowNameRef = useRef('New workflow');

  // Make sure both dashboard lists are populated whenever the editor is open
  // — the live name-uniqueness check now spans workflows AND agents so a
  // workflow can never share a name with an agent (or vice versa).
  //
  // IMPORTANT: only depend on `view` here. Including `existingAgents` /
  // `existingWorkflows` causes an infinite refetch loop — each loadAgents()
  // call does `set({ agents: data })`, which produces a new array reference
  // even when the contents are identical, which re-runs the effect, which
  // calls loadAgents() again. The store already dedupes/throttles internally
  // (see agentsStore.js), so it's safe to re-run on view changes only.
  useEffect(() => {
    if (view !== 'editor') return;
    if (!existingWorkflows || existingWorkflows.length === 0) loadWorkflows();
    if (!existingAgents || existingAgents.length === 0) loadAgents();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);

  // Dirty-check + lastSavedRef snapshot of the live editor state. Delegates to
  // the module-level buildSnapshot so all seed sites share one shape.
  const buildAutosaveSnapshot = useCallback(
    () => buildSnapshot(workflowName, nodes, edges, workflowKnowledge),
    [workflowName, nodes, edges, workflowKnowledge]);

  const saveCurrentWorkflow = useCallback(async () => {
    // Template-edit branch — write to /templates/{id} instead of /workflows.
    // The template editor doesn't enforce the workflow-vs-agent uniqueness
    // rule because templates have their own ID space and never appear in
    // the workflow picker.
    if (editingTemplateId) {
      setSaveStatus('saving');
      try {
        const { updateTemplate } = useTemplateAdminStore.getState();
        const updated = await updateTemplate(editingTemplateId, {
          name: workflowName,
          graphData: { nodes: sanitizeNodesForPersist(nodes), edges },
        });
        if (updated) {
          lastSavedRef.current = buildAutosaveSnapshot();
          setSaveStatus('saved');
        } else {
          setSaveStatus('unsaved');
        }
      } catch {
        setSaveStatus('unsaved');
      }
      return;
    }

    if (currentWorkflowId) {
      // Only a FORMAT error blocks autosave (empty/charset/length). Duplicate
      // names don't block — they're auto-resolved to a free "<name> N" on commit
      // and the backend doesn't enforce name uniqueness anyway.
      const err = validateEntityName(workflowName, 'workflow');
      if (err) {
        setWorkflowNameError(err);
        setSaveStatus('unsaved');
        return;
      }
      setSaveStatus('saving');
      try {
        const updated = await updateWorkflow(currentWorkflowId, {
          name: workflowName,
          graphData: { nodes: sanitizeNodesForPersist(nodes), edges },
          knowledge: workflowKnowledge || { mode: KB_MODE_NONE },
          expected_updated_at: workflowUpdatedAtRef.current,
        });
        if (!updated) throw new Error('Workflow save failed');
        workflowUpdatedAtRef.current = updated.updated_at || updated.updatedAt || workflowUpdatedAtRef.current;
        lastSavedRef.current = buildAutosaveSnapshot();
        setSaveStatus('saved');
      } catch (error) {
        if (error?.status === 409) {
          await loadWorkflows();
        }
        setSaveStatus('unsaved');
      }
    }
  }, [editingTemplateId, currentWorkflowId, workflowName, nodes, edges, workflowKnowledge, updateWorkflow, buildAutosaveSnapshot, loadWorkflows]);

  useEffect(() => {
    const handleBeforeUnload = () => { saveCurrentWorkflow(); };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [saveCurrentWorkflow]);

  // Immediate auto-save: persist the workflow on the next microtask after
  // any change to nodes, edges, or the workflow name. Previously this used
  // a 1.5s debounce, which created a window where clicking Run right after
  // changing the model picker would snapshot the OLD modelName (the in-
  // flight save hadn't landed yet) — the symptom was the SwarmRuntime
  // logging a model the user thought they had just changed away from.
  // Using ``setTimeout(..., 0)`` (vs. a synchronous call inside useEffect)
  // lets React commit the state update first so the snapshot we build is
  // the post-change one. ``clearTimeout`` still wins coalescing across
  // rapid edits, so a burst of keystrokes still collapses to a single PUT.
  useEffect(() => {
    if (view !== 'editor') return;
    // Either a workflow OR a template must be open for autosave to mean
    // anything. When both are null the editor is in scratch mode.
    if (!currentWorkflowId && !editingTemplateId) return;
    // Defer autosave until the drag finishes. ReactFlow tags each node
    // with `dragging: true` while the user is moving it and fires position
    // changes on every animation frame. Saving (and toggling saveStatus)
    // on every frame caused the save-dot in the header to flicker between
    // 'unsaved'/'saving' and shift the Edit/Preview toggle next to it.
    // The effect re-runs once `dragging` flips back to false, so the final
    // position is still persisted — we just collapse the burst into one PUT.
    if (nodes.some((n) => n.dragging)) return;
    const current = buildAutosaveSnapshot();
    if (lastSavedRef.current && current === lastSavedRef.current) return;

    // Functional setter so a no-op transition returns the same reference
    // and React skips the re-render.
    setSaveStatus((prev) => (prev === 'saved' ? 'unsaved' : prev));
    const timeout = setTimeout(() => { saveCurrentWorkflow(); }, 0);
    return () => clearTimeout(timeout);
  }, [view, currentWorkflowId, editingTemplateId, nodes, buildAutosaveSnapshot, saveCurrentWorkflow]);

  // Enter the canvas editor with the Chat Panel visible. Opening a workflow
  // or template lands the user in preview so they can start talking to the
  // AI immediately; node configuration is a secondary action behind the
  // Edit toggle.
  const openEditorInPreview = () => {
    setView('editor');
    setMode('preview');
  };

  // Returns true when the workflow was actually opened, false when the user
  // declined the "another run is in flight" confirm. Callers MUST honour the
  // false case: several of them chain follow-up navigation (e.g. flipping to
  // edit mode) that would otherwise still run after a cancel and drag the
  // user onto the very screen they just backed out of.
  const handleOpenWorkflow = (workflow) => {
    // If a different workflow is currently executing, opening another one
    // would wipe its in-memory chat slice and abort the SSE stream. Surface
    // the running run instead and let the user finish it (or stop it) before
    // switching contexts. The toast also points at the live workflow, so
    // this just keeps the UI consistent with what the toast advertises.
    const storeState = useWorkflowStore.getState();
    if (
      storeState.isExecuting
      && storeState.workflowId
      && storeState.workflowId !== workflow.id
    ) {
      const runningName = storeState.workflowName || 'a workflow';
      const proceed = window.confirm(
        `"${runningName}" is still running. Opening another workflow will stop that run. Continue?`
      );
      if (!proceed) {
        // User cancelled — stay wherever they are and keep the run alive.
        return false;
      }
      // User chose to switch — explicitly stop the run before we overwrite
      // the store with the new workflow's graph.
      storeState.setExecuting(false);
      storeState.clearExecutionState();
    }
    setCurrentWorkflowId(workflow.id);
    workflowUpdatedAtRef.current = workflow.updated_at || workflow.updatedAt || null;
    setEditingTemplateId(null);    // leaving any prior template-edit session
    const name = workflow.name || 'New workflow';
    setWorkflowName(name);
    setStoreWorkflowName(name);
    setStoreWorkflowId(workflow.id);
    // Snapshot the row's original name so the uniqueness check ignores the
    // self-row when comparing against the dashboard list.
    initialWorkflowNameRef.current = name;

    // Older rows that predate the ``workflows.knowledge`` column return
    // without the field — fall back to the canonical default.
    const loadedKnowledge = workflow.knowledge || { mode: KB_MODE_NONE };
    setWorkflowKnowledge(loadedKnowledge);

    if (workflow.graphData) {
      setNodes(workflow.graphData.nodes || []);
      setEdges(workflow.graphData.edges || []);
    } else {
      resetWorkflow();
      setStoreWorkflowId(workflow.id);
      setStoreWorkflowName(name);
    }

    // Seed the dirty-check baseline via buildSnapshot so it matches the autosave
    // snapshot exactly — otherwise the canvas mount (React Flow adds `measured`)
    // would look like a change and fire a spurious save.
    lastSavedRef.current = buildSnapshot(
      name, workflow.graphData?.nodes, workflow.graphData?.edges, loadedKnowledge);
    setSaveStatus('saved');

    openEditorInPreview();
    return true;
  };

  // Seed the editor store from a template's graph and land in preview. Shared by
  // the template-EDIT flow (handleOpenTemplate) and the chat-only PREVIEW flow
  // (handlePreviewTemplate); the caller sets the mode-distinguishing ids around
  // this. buildSnapshot with a null knowledge blob matches buildAutosaveSnapshot
  // for templates, so the canvas-mount `measured` diff can't fire a spurious
  // autosave on open.
  const seedTemplateIntoEditor = (template) => {
    const name = template.name || 'Template';
    setWorkflowName(name);
    setStoreWorkflowName(name);
    initialWorkflowNameRef.current = name;

    const graph = template.graphData || template.graph_data || { nodes: [], edges: [] };
    setNodes(graph.nodes || []);
    setEdges(graph.edges || []);

    lastSavedRef.current = buildSnapshot(name, graph.nodes, graph.edges, null);
    setSaveStatus('saved');

    openEditorInPreview();
  };

  // Open a template inside the canvas editor in "template-save" mode. Any
  // graph edits autosave to PUT /templates/{id}. Setting editingTemplateId
  // (and clearing currentWorkflowId) is what tells `saveCurrentWorkflow`
  // to take the template branch. Optional feature — remove this handler
  // along with the rest of the template editor blocks.
  const handleOpenTemplate = (template) => {
    setCurrentWorkflowId(null);
    workflowUpdatedAtRef.current = null;
    setStoreWorkflowId(null);
    setEditingTemplateId(template.id);
    seedTemplateIntoEditor(template);
  };

  // Open a template as a CHAT-ONLY preview (the "Try it" flow). Leaving both
  // `currentWorkflowId` and `editingTemplateId` null keeps autosave inert so
  // nothing is cloned/persisted; the canvas/palette are hidden (see EditorShell)
  // and the header "Edit" toggle promotes this into a real workflow.
  const handlePreviewTemplate = (template) => {
    setCurrentWorkflowId(null);
    workflowUpdatedAtRef.current = null;
    setEditingTemplateId(null);
    setPreviewingTemplate(template);
    // Scope chat threads/history to this template id.
    setStoreWorkflowId(template.id);
    seedTemplateIntoEditor(template);
  };

  // Promote the current template preview into a real, editable workflow. Reuses
  // the existing clone endpoint (useTemplate) and the normal open-in-editor
  // flow, then flips into edit mode so the nodes/palette appear.
  const handleEditFromTemplatePreview = async () => {
    if (!previewingTemplate) return;
    const wf = await useTemplate(previewingTemplate.id);
    if (!wf) return;
    // Clear the preview flag only once handleOpenWorkflow confirms the switch.
    // Doing it up-front used to tear down the chat-only preview shell before
    // the confirm was answered, so cancelling left the user on the full editor
    // (view was already 'editor' here) — the bug this guard closes.
    if (!handleOpenWorkflow(wf)) return;
    setPreviewingTemplate(null);
    setMode('edit');
  };

  // Open an agent template as a CHAT-ONLY preview (the "Use" flow). No clone
  // is created — the backend resolves the template by id at chat time — so
  // nothing lands in "My Agents" and the Deploy button stays hidden until the
  // user promotes the template into a real agent via handlePromoteAgentTemplate.
  const handlePreviewAgentTemplate = (template) => {
    setEditingAgent(null);
    setAgentEditorMode('preview');
    setPreviewingAgentTemplate(template);
  };

  // Promote the current agent-template preview into a real, editable agent.
  // Reuses the existing clone endpoint (useAgentTemplate) and opens the
  // resulting agent in edit mode so the config form appears. Mirrors the
  // workflow handleEditFromTemplatePreview flow.
  const handlePromoteAgentTemplate = async (template) => {
    if (!template) return;
    const { useAgentTemplate } = useAgentsStore.getState();
    const agent = await useAgentTemplate(template.id);
    setPreviewingAgentTemplate(null);
    if (agent) {
      setAgentEditorMode('edit');
      setEditingAgent(agent);
    }
  };

  const handleBackToDashboard = async () => {
    await saveCurrentWorkflow();
    // If a workflow run is in progress, keep the editor's identity intact
    // (currentWorkflowId, workflow name, store workflow id, nodes/edges,
    // chat slice). The dashboard surfaces a "<name> is running" toast that
    // routes the user back into this exact editor in preview mode, and the
    // ChatPanel keeps streaming into the store while the user browses the
    // dashboard. Wiping state here would orphan the SSE handler and lose
    // every message that arrived after the user left.
    const { isExecuting } = useWorkflowStore.getState();
    if (!isExecuting) {
      setCurrentWorkflowId(null);
      workflowUpdatedAtRef.current = null;
      setEditingTemplateId(null);   // also exit template-edit mode
      setPreviewingTemplate(null);  // also exit template chat-preview mode
      setWorkflowName('New workflow');
      setStoreWorkflowName('New workflow');
      setStoreWorkflowId(null);
      resetWorkflow();
    }
    setView('dashboard');
  };

  // Called by RunningWorkflowToast when the user clicks "Open" — return
  // them to the preview pane of the still-executing workflow.
  const handleResumeRunningWorkflow = useCallback(() => {
    openEditorInPreview();
  }, []);

  // One-shot: on first mount, reopen the editor the user had open before a
  // reload. The stored pointer is just {kind,id,mode}; the DB is re-fetched
  // and the id re-validated (so a deleted entity falls back to the dashboard,
  // and a foreign id from another user on a shared browser simply isn't found
  // in this user's list). Reuses the normal open handlers so all the
  // graph/knowledge/name snapshotting stays identical to a manual open.
  // `restoreDoneRef` guards against re-running once a restore has actually
  // COMPLETED (not merely started). React StrictMode mounts→unmounts→remounts
  // in dev, cancelling the first run's async work; keying the guard on
  // completion lets the remount retry instead of skipping restore entirely.
  const restoreDoneRef = useRef(false);
  useEffect(() => {
    if (restoreDoneRef.current) return;

    let cancelled = false;
    (async () => {
      try {
        await ensureUserNamespace();
        if (cancelled) return;
        const pointer = loadOpenEditor();
        // Flip the persist gate only after the pointer is read, so the persist
        // effect can't clear the stored pointer out from under us mid-restore.
        editorRestoredRef.current = true;
        restoreDoneRef.current = true;
        if (!pointer) return;

        if (pointer.kind === 'workflow') {
          try {
            await loadWorkflows();
          } catch {
            return;
          }
          if (cancelled) return;
          const row = (useDashboardStore.getState().workflows || []).find((w) => w.id === pointer.id);
          if (!row) { clearOpenEditor(); return; }
          // Same contract as the other call sites: a declined confirm must not
          // fall through to the mode/selected-node restore below.
          if (!handleOpenWorkflow(row)) return;
          if (pointer.mode === 'edit') setMode('edit');
          // Reopen the config panel for the node the user had selected, but
          // only if that node still exists in the reloaded graph.
          const storedNode = loadSelectedNode(pointer.id);
          if (storedNode) {
            const graphNodes = row.graphData?.nodes || [];
            if (graphNodes.some((n) => n.id === storedNode)) setSelectedNode(storedNode);
          }
        } else if (pointer.kind === 'agent') {
          try {
            await loadAgents();
          } catch {
            return;
          }
          if (cancelled) return;
          const agent = (useAgentsStore.getState().agents || []).find((a) => a.id === pointer.id);
          if (!agent) { clearOpenEditor(); return; }
          setAgentEditorMode(pointer.mode === 'preview' ? 'preview' : 'edit');
          setEditingAgent(agent);
        }
      } finally {
        if (!cancelled) setRestoring(false);
      }
    })();

    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Validation options for the workflow editor's name field.
  //
  // Uniqueness is scoped to WORKFLOWS ONLY — a workflow may share a name with an
  // agent. The subflow picker disambiguates by kind+id (not bare name), so there
  // is no ambiguity to guard against. Duplicates are never blocked: they're
  // auto-resolved to a free "<name> N" on commit (see commitWorkflowName).
  const buildWorkflowNameValidatorOpts = () => ({
    existingItems: (existingWorkflows || []).map((w) => ({ id: w.id, name: w.name })),
    currentId: currentWorkflowId || '',
  });

  const handleNameChange = (e) => {
    const next = e.target.value;
    setWorkflowName(next);
    setStoreWorkflowName(next);
    // Live: only surface FORMAT errors (empty/charset/length) as the user types.
    // Duplicate names are not flagged here — they're auto-resolved on commit —
    // so the field never rewrites mid-keystroke.
    setWorkflowNameError(validateEntityName(next, 'workflow'));
  };

  // Commit the name (on blur / Enter): if it's format-valid but collides with an
  // existing workflow, silently bump it to the next free "<name> N" so the user
  // is never blocked by a "Name already in use." error.
  const commitWorkflowName = () => {
    setIsEditingName(false);
    const formatErr = validateEntityName(workflowName, 'workflow');
    if (formatErr) {
      setWorkflowNameError(formatErr);
      return;
    }
    const opts = buildWorkflowNameValidatorOpts();
    const free = suggestFreeName(workflowName, opts.existingItems, opts.currentId);
    if (free !== workflowName.trim()) {
      setWorkflowName(free);
      setStoreWorkflowName(free);
    }
    setWorkflowNameError(null);
  };

  const handleNameBlur = () => { commitWorkflowName(); };
  const handleNameKeyDown = (e) => {
    if (e.key === 'Enter') { commitWorkflowName(); }
  };

  // While the one-shot restore resolves, show a neutral splash instead of the
  // dashboard so a reload that reopens an editor doesn't flash the dashboard
  // first. Only reached when a stored pointer exists (see `restoring` init).
  if (restoring) {
    return (
      <div
        data-ac=""
        className="app-container"
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}
      >
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#4f46e5" strokeWidth="2.5" strokeLinecap="round" className="save-dot-spin" aria-label="Loading">
          <path d="M21 12a9 9 0 1 1-6.219-8.56" />
        </svg>
      </div>
    );
  }

  // Agent editor full-screen view. Renders for either a saved agent being
  // edited (editingAgent) OR a chat-only template preview
  // (previewingAgentTemplate). The two are mutually exclusive — promoting a
  // template preview clears previewingAgentTemplate and sets editingAgent.
  //
  // The ``key`` forces a full remount when transitioning between the two
  // shapes (template-preview → promoted real agent, or vice-versa). Without
  // it the editor's internal ``mode``/``savedId``/``form`` state — which only
  // initializes from props on first mount — would stay stuck in the
  // template-preview values (mode='preview', savedId=null), so the promoted
  // agent would wrongly show the chat pane instead of the config form and
  // the Deploy button wouldn't appear until a manual re-open.
  if (editingAgent !== null || previewingAgentTemplate) {
    const templatePreview = previewingAgentTemplate;
    const editorKey = templatePreview
      ? `template::${templatePreview.id}`
      : `agent::${editingAgent?.id || 'new'}`;
    // Normalize a template into the agent shape the editor expects, so the
    // editor can read ``agent.*`` uniformly whether it's a real saved agent
    // or a chat-only template preview. The editor stays agnostic of the
    // template's field set.
    const agentProp = editingAgent || (templatePreview ? {
      id: null,
      name: templatePreview.name || 'New Agent',
      description: templatePreview.description || '',
      instructions: templatePreview.instructions || '',
      provider: templatePreview.provider || 'custom',
      model_name: templatePreview.model_name || '',
      temperature: templatePreview.temperature ?? 0.7,
      max_tokens: templatePreview.max_tokens ?? 2048,
      top_p: templatePreview.top_p ?? 1.0,
      tools: templatePreview.tools || [],
      skills: templatePreview.skills || [],
    } : null);
    return (
      <div data-ac="" className="agent-editor-view" style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <AgentEditor
          key={editorKey}
          agent={agentProp}
          initialMode={agentEditorMode}
          onModeChange={setAgentEditorMode}
          templatePreview={templatePreview}
          onPromoteTemplate={handlePromoteAgentTemplate}
          onBack={() => {
            setEditingAgent(null);
            setPreviewingAgentTemplate(null);
            loadAgents();
          }}
        />
      </div>
    );
  }

  // While a workflow is actively executing we keep the editor mounted even
  // when the user is on the dashboard. ChatPanel's SSE reader needs to stay
  // alive so the in-flight stream keeps populating the store — otherwise
  // the moment the user navigates away the run is silently aborted (the
  // panel's unmount-time effect calls abortRef.current.abort()).
  // The editor is hidden with display:none in this case; the dashboard
  // renders normally on top.
  //
  // A run paused at an HITL gate counts as "in flight" here even though
  // `isExecuting` is false: unmounting would discard the pending approval
  // card and abort the follow-up /resume-stream.
  const showDashboard = view === 'dashboard';
  const runInFlight = isExecuting || !!chatHitlRequest;
  const keepEditorMounted = !showDashboard || (runInFlight && !!currentWorkflowId);

  // NOTE: TriggerNotifications is intentionally NOT rendered inside the
  // workflow editor. While the user is on the canvas, the ChatPanel
  // surfaces triggered runs inline (as ⏰ Scheduled run · <IST> bubbles)
  // so the floating bell would just duplicate the same information.
  // The bell still lives on the dashboard + agent editor surfaces.
  return (
    <>
      {showDashboard && (
        <div data-ac="" className="app-container dashboard-view has-topbar animate-fade-in">
          <AppTopBar section={section} onSectionChange={setSection} />
          <div className="dashboard-content-area">
            {section === 'workflows' && (
              <WorkflowsDashboard
                onOpenWorkflow={handleOpenWorkflow}
                onCreateNew={handleOpenWorkflow}
                onOpenTemplate={handleOpenTemplate}
                onPreviewTemplate={handlePreviewTemplate}
              />
            )}
            {section === 'agents' && (
              <AgentsDashboard
                onOpenAgent={(agent, initialMode = null) => {
                  const mode = initialMode || (agent?.id ? 'preview' : 'edit');
                  setAgentEditorMode(mode);
                  setEditingAgent(agent);
                }}
                onPreviewTemplate={handlePreviewAgentTemplate}
              />
            )}
            {section === 'skills' && <SkillsDashboard />}
            {section === 'tools' && <ToolsDashboard />}
          </div>
          {/* Surfaces the in-flight workflow run while the user is on the
              dashboard. Clicking "Open" routes back into the editor's
              preview pane where ChatPanel has been streaming all along. */}
          <RunningWorkflowToast onOpen={handleResumeRunningWorkflow} />
        </div>
      )}
      {keepEditorMounted && (
        <EditorShell
          hidden={showDashboard}
          handleBackToDashboard={handleBackToDashboard}
          isEditingName={isEditingName}
          setIsEditingName={setIsEditingName}
          workflowName={workflowName}
          workflowNameError={workflowNameError}
          handleNameChange={handleNameChange}
          handleNameBlur={handleNameBlur}
          handleNameKeyDown={handleNameKeyDown}
          currentWorkflowId={currentWorkflowId}
          saveStatus={saveStatus}
          mode={mode}
          setMode={setMode}
          selectedNodeId={selectedNodeId}
          chatWidth={chatWidth}
          handleResizeMouseDown={handleResizeMouseDown}
          isTemplatePreview={!!previewingTemplate}
          onEditTemplate={handleEditFromTemplatePreview}
        />
      )}
    </>
  );
}

// Editor shell extracted so it can be conditionally rendered side-by-side
// with the dashboard (hidden via display:none) while a workflow run is
// still in flight. Keeps ChatPanel's SSE reader and abort controller alive
// across dashboard ↔ editor navigation.
function EditorShell({
  hidden,
  handleBackToDashboard,
  isEditingName,
  setIsEditingName,
  workflowName,
  workflowNameError,
  handleNameChange,
  handleNameBlur,
  handleNameKeyDown,
  currentWorkflowId,
  saveStatus,
  mode,
  setMode,
  selectedNodeId,
  chatWidth,
  handleResizeMouseDown,
  isTemplatePreview,
  onEditTemplate,
}) {
  // Floating header (back button, name, Edit/Preview toggle) used by the CANVAS
  // editor. It's absolutely positioned over the canvas — fine there since the
  // canvas has empty space at the top, but it would overlap chat text, so the
  // template chat-preview uses a solid top bar instead (see below).
  const editorHeader = (
            <div className="editor-header">
              <button className="back-to-dashboard-btn" onClick={handleBackToDashboard} title="Back to Dashboard">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M19 12H5M12 19l-7-7 7-7" />
                </svg>
              </button>
              <div className="workflow-name-container">
                {isEditingName ? (
                  <input
                    type="text"
                    className={`workflow-name-input${workflowNameError ? ' workflow-name-input-error' : ''}`}
                    value={workflowName}
                    onChange={handleNameChange}
                    onBlur={handleNameBlur}
                    onKeyDown={handleNameKeyDown}
                    aria-invalid={workflowNameError ? 'true' : 'false'}
                    autoFocus
                  />
                ) : (
                  <button className="workflow-name-btn" onClick={() => setIsEditingName(true)} title="Click to edit name">
                    {workflowName}
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                    </svg>
                  </button>
                )}
                {workflowNameError && (
                  <div className="workflow-name-error" role="alert">
                    {workflowNameError}
                  </div>
                )}
              </div>
              {/* Governance status pill (e.g. "Awaiting Approval") shown at the
                  top of the editor beside the workflow name. */}
              {currentWorkflowId && workflowName && (
                <StatusBadge entityType="workflows" name={workflowName} style={{ marginLeft: 4 }} />
              )}
              {/* Save indicator is ALWAYS mounted (when a workflow is open) so
                  it never causes a layout shift on the header pill — dragging
                  nodes flips saveStatus 'saved' → 'unsaved' → 'saving' → 'saved'
                  many times per second, and previously the dot mounted/unmounted
                  on every transition, jiggling the Edit/Preview toggle to the
                  right of it. We now collapse the visual to an invisible
                  zero-opacity dot when 'saved' and keep its slot reserved. */}
              {currentWorkflowId && (() => {
                const saveLabel = saveStatus === 'saving'
                  ? 'Saving…'
                  : saveStatus === 'unsaved'
                    ? 'Unsaved changes'
                    : 'All changes saved';
                return (
                  <span
                    className={`save-dot save-dot--${saveStatus}`}
                    role="status"
                    aria-live="polite"
                    title={saveLabel}
                  >
                    {saveStatus === 'saving' && (
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" className="save-dot-spin">
                        <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                      </svg>
                    )}
                    <span className="sr-only">{saveLabel}</span>
                  </span>
                );
              })()}
              {/* Submit this saved workflow to its department manager for
                  approval. Renders only while (re)submission is warranted. */}
              {currentWorkflowId && workflowName && (
                <SubmitApprovalButton entityType="workflows" name={workflowName} />
              )}
              <div className="floating-toggle floating-toggle--inline">
                <button className={`mode-btn ${mode === 'edit' ? 'active' : ''}`} onClick={() => setMode('edit')} title="Edit Mode">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                  </svg>
                </button>
                <button className={`mode-btn ${mode === 'preview' ? 'active' : ''}`} onClick={() => setMode('preview')} title="Preview Mode">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <polygon points="5 3 19 12 5 21 5 3" />
                  </svg>
                </button>
              </div>
            </div>
  );

  // Solid top bar for the chat-only template preview. Reuses the Agent editor's
  // topbar/toggle classes so it looks identical and, unlike editorHeader, it
  // occupies its own row (not absolutely positioned) so chat text never sits
  // behind it. The toggle promotes the template into an editable workflow.
  const templatePreviewTopBar = (
    <div className="agent-editor-topbar">
      <button className="back-to-dashboard-btn" onClick={handleBackToDashboard} title="Back to Dashboard">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M19 12H5M12 19l-7-7 7-7" />
        </svg>
      </button>
      <div className="workflow-name-container">
        <span className="workflow-name-btn" style={{ cursor: 'default' }}>{workflowName}</span>
      </div>
      <div className="agent-editor-toggle">
        <button className="mode-btn active" onClick={onEditTemplate} title="Edit — creates an editable copy">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
          </svg>
          Edit
        </button>
      </div>
    </div>
  );

  if (isTemplatePreview) {
    return (
      <ReactFlowProvider>
        <div
          data-ac=""
          className="app-container animate-fade-in"
          style={hidden ? { display: 'none' } : { height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
          aria-hidden={hidden ? 'true' : 'false'}
        >
          {templatePreviewTopBar}
          <div className="chat-panel-mount chat-panel-mount--full">
            <ChatPanel
              isActive={!hidden}
              style={{ width: '100%', minWidth: 0, maxWidth: 'none', flex: 1 }}
            />
          </div>
        </div>
      </ReactFlowProvider>
    );
  }

  return (
    <ReactFlowProvider>
      <div
        data-ac=""
        className="app-container animate-fade-in"
        style={hidden ? { display: 'none' } : undefined}
        aria-hidden={hidden ? 'true' : 'false'}
      >
        <div className="main-content">
          {mode === 'edit' && <Sidebar />}
          <div className="canvas-wrapper">
            <Canvas onRequestEditMode={() => setMode('edit')} />
            <div className="canvas-noise-overlay" />
            {editorHeader}
          </div>
          {mode === 'edit' && selectedNodeId && <ConfigPanel />}
          {/* Kept mounted across edit/preview swaps so the SSE reader and
              streaming state survive when the user clicks a node mid-run. */}
          <div className={`chat-panel-mount${mode === 'preview' ? '' : ' chat-panel-mount--hidden'}`}>
            <div className="chat-resize-handle" onMouseDown={handleResizeMouseDown} />
            <ChatPanel
              isActive={!hidden && mode === 'preview'}
              style={{ width: chatWidth, minWidth: chatWidth, maxWidth: chatWidth }}
            />
          </div>
        </div>
      </div>
    </ReactFlowProvider>
  );
}

export { ErrorBoundary as AppErrorBoundary };
export default App;
