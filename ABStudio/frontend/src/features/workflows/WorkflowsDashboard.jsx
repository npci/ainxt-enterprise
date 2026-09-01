// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState, useMemo, useCallback } from 'react';
import useDashboardStore from '../../store/dashboardStore';
import WorkflowCard from './WorkflowCard';
import WorkflowFactoryChat from './WorkflowFactoryChat';
import TriggerModal from '../triggers/TriggerModal';
import useTemplateAdminStore from '../../store/templateAdminStore';
import TemplateCardMenu from '../templates/TemplateCardMenu';
import TemplateCreateModal from '../templates/TemplateCreateModal';
import TemplatesEmptyState from '../../components/common/TemplatesEmptyState';
import { CATEGORY_OPTIONS } from './templateCategories';

const CATEGORY_FILTERS = CATEGORY_OPTIONS.map((c) => [c, c]);

function CollapsibleSection({ title, count, defaultOpen = true, children }) {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <div className="wfd-section">
            <button className="wfd-section-toggle" onClick={() => setOpen(o => !o)} aria-expanded={open}>
                <span className="wfd-section-title">{title}</span>
                {count != null && <span className="wfd-section-count">{count}</span>}
                <svg className={`wfd-chevron${open ? ' wfd-chevron--open' : ''}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                    <polyline points="6 9 12 15 18 9" />
                </svg>
            </button>
            {open && <div className="wfd-section-body">{children}</div>}
        </div>
    );
}


function Dashboard({ onOpenWorkflow, onCreateNew, onOpenTemplate, onPreviewTemplate }) {
    const {
        workflows, templates, isLoading, error: storeError,
        loadWorkflows, loadTemplates, createWorkflow, deleteWorkflow, duplicateWorkflow, clearError,
    } = useDashboardStore();

    const templatesEditable = useTemplateAdminStore((s) => s.isEditable);
    const loadAdminStatus   = useTemplateAdminStore((s) => s.loadStatus);

    const [search, setSearch]                         = useState('');
    const [templateVisibility, setTemplateVisibility] = useState('all');
    const [templateCategory, setTemplateCategory]     = useState('all');
    const [showWorkflowFactory, setShowWorkflowFactory] = useState(false);
    const [showCreateTemplate, setShowCreateTemplate]   = useState(false);
    const [triggerWorkflow, setTriggerWorkflow]         = useState(null);

    useEffect(() => { loadWorkflows(); loadTemplates(); loadAdminStatus(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        if (!storeError) return;
        const t = setTimeout(() => { clearError(); loadWorkflows(); loadTemplates(); }, 5000);
        return () => clearTimeout(t);
    }, [storeError]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        const h = () => { loadWorkflows(); loadTemplates(); };
        window.addEventListener('agent-desktop-auth-ready', h);
        return () => window.removeEventListener('agent-desktop-auth-ready', h);
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    const templateIds = useMemo(() => templates.map((t) => t.id), [templates]);

    const nextDefaultName = useCallback(() => {
        const taken = new Set((workflows || []).map(w => (w.name || '').trim().toLowerCase()));
        if (!taken.has('new workflow')) return 'New workflow';
        for (let n = 2; n < 1000; n++) { const c = `New workflow ${n}`; if (!taken.has(c.toLowerCase())) return c; }
        return `New workflow ${Date.now()}`;
    }, [workflows]);

    const handleCreateNew = async () => {
        const wf = await createWorkflow({
            name: nextDefaultName(),
            graphData: {
                nodes: [
                    { id: 'start-default', type: 'start', position: { x: 100, y: 200 }, data: { label: 'Start' } },
                    { id: 'agent-default', type: 'agent', position: { x: 300, y: 200 }, data: { name: '', instructions: '', provider: 'custom', apiKey: '', modelName: '', temperature: 0.7, maxTokens: 2048, topP: 1.0, baseUrl: '' } },
                    { id: 'end-default', type: 'end', position: { x: 520, y: 200 }, data: { label: 'End' } },
                ],
                edges: [
                    { id: 'edge-start-agent', source: 'start-default', target: 'agent-default' },
                    { id: 'edge-agent-end', source: 'agent-default', target: 'end-default' },
                ],
            },
        });
        if (wf && onCreateNew) onCreateNew(wf);
    };

    const handleWorkflowGenerated = async (wf) => {
        setShowWorkflowFactory(false);
        await loadWorkflows();
        if (wf && onOpenWorkflow) onOpenWorkflow(wf);
    };



    const filteredWorkflows = useMemo(() => {
        let items = workflows;
        if (search.trim()) { const q = search.trim().toLowerCase(); items = items.filter(w => w.name?.toLowerCase().includes(q) || w.description?.toLowerCase().includes(q)); }
        return [...items].sort((a, b) => new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0));
    }, [workflows, search]);

    const filteredTemplates = useMemo(() => {
        let items = templates;
        if (templateVisibility === 'private') items = items.filter(t => t.visibility === 'private');
        else if (templateVisibility === 'public') items = items.filter(t => t.visibility !== 'private');
        if (templateCategory !== 'all') items = items.filter(t => t.category === templateCategory);
        if (search.trim()) { const q = search.trim().toLowerCase(); items = items.filter(t => t.name?.toLowerCase().includes(q) || t.description?.toLowerCase().includes(q) || t.category?.toLowerCase().includes(q)); }
        return items;
    }, [templates, search, templateVisibility, templateCategory]);

    return (
        <div className="wfd-page animate-fade-in">
            {storeError && (
                <div className="dashboard-error-toast" role="alert">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" style={{ flexShrink: 0 }}><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
                    <span>{storeError} — retrying in 5s…</span>
                    <button className="dashboard-error-retry" onClick={() => { clearError(); loadWorkflows(); loadTemplates(); }}>Retry now</button>
                    <button className="dashboard-error-close" onClick={clearError} aria-label="Dismiss">×</button>
                </div>
            )}

            <div className="wfd-header">
                <div>
                    <h1 className="wfd-title">Workflow Builder</h1>
                    <p className="wfd-subtitle">Build and orchestrate your AI agent pipelines.</p>
                </div>
                <div className="wfd-header-actions">
                    <button className="wfd-btn-primary" onClick={() => setShowWorkflowFactory(true)}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
                        Create with AI
                    </button>
                    <button className="wfd-btn-secondary" onClick={handleCreateNew}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                        New Workflow
                    </button>
                </div>
            </div>

            <div className="wfd-search-wrap">
                <svg className="wfd-search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></svg>
                <input className="wfd-search" type="text" placeholder="Search workflows and templates…" value={search} onChange={e => setSearch(e.target.value)} />
                {search && (
                    <button className="wfd-search-clear" onClick={() => setSearch('')} aria-label="Clear">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                    </button>
                )}
            </div>

            <CollapsibleSection title="My Workflows" count={workflows.length} defaultOpen={false}>
                {isLoading ? (
                    <div className="wfd-tile-grid">
                        {[1,2,3].map(i => <div key={i} className="wfd-skeleton-tile" />)}
                    </div>
                ) : filteredWorkflows.length === 0 ? (
                    <div className="wfd-empty-small">
                        <strong>{search ? 'No workflows match your search.' : 'No workflows yet.'}</strong>
                        <span>{search ? '' : 'Create one with AI or start from scratch.'}</span>
                    </div>
                ) : (
                    <div className="wfd-tile-grid">
                        {filteredWorkflows.map((wf, i) => (
                            <WorkflowCard
                                key={wf.id}
                                className={`animate-slide-in-up stagger-${(i % 5) + 1}`}
                                workflow={wf}
                                onClick={() => onOpenWorkflow && onOpenWorkflow(wf)}
                                onDelete={() => deleteWorkflow(wf.id)}
                                onDuplicate={() => duplicateWorkflow(wf.id)}
                                onTrigger={setTriggerWorkflow}
                            />
                        ))}
                    </div>
                )}
            </CollapsibleSection>

            <div className="wfd-templates-header">
                <div>
                    <h2 className="wfd-templates-title">Workflow Templates</h2>
                    <p className="wfd-templates-subtitle">Pre-built workflows ready to customize.</p>
                </div>
                {templatesEditable && (
                    <button className="wfd-btn-secondary" onClick={() => setShowCreateTemplate(true)}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                        New Template
                    </button>
                )}
            </div>

            <div className="wfd-filters">
                {[['all','All'],['public','Public'],['private','Department']].map(([v,l]) => (
                    <button key={v} className={`wfd-chip${templateVisibility===v?' wfd-chip--active':''}`} onClick={() => setTemplateVisibility(v)}>{l}</button>
                ))}
                <span className="wfd-filter-sep" />
                {CATEGORY_FILTERS.map(([v,l]) => (
                    <button key={v} className={`wfd-chip${templateCategory===v?' wfd-chip--active':''}`} onClick={() => setTemplateCategory(c => c===v?'all':v)}>{l}</button>
                ))}
            </div>

            {filteredTemplates.length === 0 ? (
                <TemplatesEmptyState filtered={!!search || templateVisibility!=='all' || templateCategory!=='all'} onReset={() => { setSearch(''); setTemplateVisibility('all'); setTemplateCategory('all'); }} />
            ) : (
                <div className="wfd-template-grid">
                    {filteredTemplates.map((item, i) => (
                        <div key={item.id} style={{ position: 'relative' }}>
                            <WorkflowCard className={`agent-template-card animate-slide-in-up stagger-${(i%5)+1}`} workflow={item} isTemplate onClick={() => onPreviewTemplate && onPreviewTemplate(item)} />
                            {templatesEditable && <TemplateCardMenu template={item} onEditGraph={(t) => onOpenTemplate && onOpenTemplate(t)} onChanged={() => loadTemplates()} />}
                        </div>
                    ))}
                </div>
            )}

            <TriggerModal open={!!triggerWorkflow} onClose={() => setTriggerWorkflow(null)} targetKind="workflow" targetId={triggerWorkflow?.id} targetName={triggerWorkflow?.name} />
            {showWorkflowFactory && <WorkflowFactoryChat onClose={() => setShowWorkflowFactory(false)} onCreated={handleWorkflowGenerated} />}
            {templatesEditable && (
                <TemplateCreateModal open={showCreateTemplate} existingIds={templateIds} onClose={() => setShowCreateTemplate(false)}
                    onCreated={(created) => { loadTemplates(); if (created && onOpenTemplate) onOpenTemplate(created); }} />
            )}
        </div>
    );
}

export default Dashboard;
