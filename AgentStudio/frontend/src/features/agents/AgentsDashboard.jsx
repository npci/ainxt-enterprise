// SPDX-License-Identifier: MIT
import { useEffect, useMemo, useRef, useState } from 'react';
import useAgentsStore from '../../store/agentsStore';
import useGovernanceStore from '../../store/governanceStore';
import AgentCard from './AgentCard';
import AgentFactoryChat from './AgentFactoryChat';
import TriggerModal from '../triggers/TriggerModal';
import TemplatesEmptyState from '../../components/common/TemplatesEmptyState';
import { suggestFreeName } from '../../utils/validateName';

const PENDING_STATES = new Set(['PENDING_APPROVAL', 'PENDING_L2']);
const VISIBILITY_FILTERS = [['all', 'All'], ['public', 'Public'], ['private', 'Department']];

function CollapsibleSection({ title, count, defaultOpen = true, children }) {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <div className="afd-section">
            <button className="afd-section-toggle" onClick={() => setOpen(o => !o)} aria-expanded={open}>
                <span className="afd-section-title">{title}</span>
                {count != null && <span className="afd-section-count">{count}</span>}
                <svg className={`afd-chevron${open ? ' afd-chevron--open' : ''}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                    <polyline points="6 9 12 15 18 9" />
                </svg>
            </button>
            {open && <div className="afd-section-body">{children}</div>}
        </div>
    );
}


function AgentsDashboard({ onOpenAgent, onPreviewTemplate }) {
    const {
        agents, agentTemplates, isLoading, error: storeError,
        loadAgents, loadAgentTemplates, deleteAgent, duplicateAgent, clearError,
    } = useAgentsStore();

    const fetchStatus = useGovernanceStore((s) => s.fetchStatus);
    const statusMap   = useGovernanceStore((s) => s.statusMap);

    const [search, setSearch]                         = useState('');
    const [templateVisibility, setTemplateVisibility] = useState('all');
    const [showFactoryChat, setShowFactoryChat]       = useState(false);
    const [triggerAgent, setTriggerAgent]             = useState(null);

    useEffect(() => { loadAgents().catch(() => {}); loadAgentTemplates(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

    const agentStatusKeys = agents.map((a) => a.name).filter(Boolean).join('|');
    useEffect(() => {
        for (const a of agents) { if (a.name) fetchStatus('agents', a.name); }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [agentStatusKeys]);

    const prevStatusesRef = useRef({});
    useEffect(() => {
        const anyPending = agents.some((a) => PENDING_STATES.has(statusMap[`agents:${a.name}`]));
        let becameApproved = false;
        for (const a of agents) {
            const cur = statusMap[`agents:${a.name}`]; const prev = prevStatusesRef.current[a.name];
            if (PENDING_STATES.has(prev) && (cur === 'APPROVED' || cur === 'PRODUCTION' || cur === 'ACTIVE')) becameApproved = true;
            prevStatusesRef.current[a.name] = cur;
        }
        if (becameApproved) { loadAgents({ force: true }).catch(() => {}); loadAgentTemplates(); }
        if (!anyPending) return undefined;
        const id = setInterval(() => { for (const a of agents) { if (PENDING_STATES.has(statusMap[`agents:${a.name}`])) fetchStatus('agents', a.name); } }, 15000);
        return () => clearInterval(id);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [statusMap, agentStatusKeys]);

    useEffect(() => {
        const h = () => loadAgents().catch(() => {});
        window.addEventListener('agent-desktop-auth-ready', h);
        return () => window.removeEventListener('agent-desktop-auth-ready', h);
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    const handleCreateNew = () => {
        const freeName = suggestFreeName('New Agent', (agents || []).map((a) => ({ id: a.id, name: a.name })));
        if (onOpenAgent) onOpenAgent({ id: null, name: freeName, description: '', instructions: '', provider: 'custom', model_name: '', api_key: '', temperature: 0.7, max_tokens: 2048, top_p: 1.0, base_url: '' });
    };



    const filteredAgents = useMemo(() => {
        let items = agents;
        if (search.trim()) { const q = search.trim().toLowerCase(); items = items.filter(a => a.name?.toLowerCase().includes(q) || a.description?.toLowerCase().includes(q)); }
        return [...items].sort((a, b) => new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0));
    }, [agents, search]);

    const filteredTemplates = useMemo(() => {
        let items = agentTemplates;
        if (templateVisibility === 'private') items = items.filter(p => p.visibility === 'private');
        else if (templateVisibility === 'public') items = items.filter(p => p.visibility !== 'private');
        if (search.trim()) { const q = search.trim().toLowerCase(); items = items.filter(p => p.name?.toLowerCase().includes(q) || p.description?.toLowerCase().includes(q) || p.category?.toLowerCase().includes(q)); }
        return items;
    }, [agentTemplates, search, templateVisibility]);

    return (
        <div className="afd-page animate-fade-in">
            {storeError && (
                <div className="dashboard-error-toast" role="alert">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" style={{ flexShrink: 0 }}><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
                    <span>{storeError}</span>
                    <button className="dashboard-error-retry" onClick={() => { clearError(); loadAgents().catch(() => {}); }}>Retry now</button>
                    <button className="dashboard-error-close" onClick={clearError} aria-label="Dismiss">×</button>
                </div>
            )}

            <div className="afd-header">
                <div>
                    <h1 className="afd-title">Agent Builder</h1>
                    <p className="afd-subtitle">Use a template, ask AI to build one, or start from scratch.</p>
                </div>
                <div className="afd-header-actions">
                    <button className="afd-btn-primary" onClick={() => setShowFactoryChat(true)}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
                        Create with AI
                    </button>
                    <button className="afd-btn-secondary" onClick={handleCreateNew}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                        New Agent
                    </button>
                </div>
            </div>

            <div className="afd-search-wrap">
                <svg className="afd-search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></svg>
                <input className="afd-search" type="text" placeholder="Search agents and templates…" value={search} onChange={e => setSearch(e.target.value)} />
                {search && (
                    <button className="afd-search-clear" onClick={() => setSearch('')} aria-label="Clear">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                    </button>
                )}
            </div>

            <CollapsibleSection title="My Agents" count={agents.length} defaultOpen={false}>
                {isLoading ? (
                    <div className="wfd-tile-grid">
                        {[1,2,3].map(i => <div key={i} className="wfd-skeleton-tile" />)}
                    </div>
                ) : filteredAgents.length === 0 ? (
                    <div className="afd-empty-small">
                        <strong>{search ? 'No agents match your search.' : 'No agents yet.'}</strong>
                        <span>{search ? '' : 'Create one with AI or start from scratch.'}</span>
                    </div>
                ) : (
                    <div className="wfd-tile-grid">
                        {filteredAgents.map((agent, i) => (
                            <AgentCard
                                key={agent.id}
                                className={`animate-slide-in-up stagger-${(i % 5) + 1}`}
                                agent={agent}
                                onClick={() => onOpenAgent && onOpenAgent(agent, 'preview')}
                                onDelete={() => deleteAgent(agent.id)}
                                onDuplicate={() => duplicateAgent(agent.id)}
                            />
                        ))}
                    </div>
                )}
            </CollapsibleSection>

            <div className="afd-templates-header">
                <div>
                    <h2 className="afd-templates-title">Agent Templates</h2>
                    <p className="afd-templates-subtitle">Pre-built agents ready to customize.</p>
                </div>
            </div>

            <div className="afd-filters">
                {VISIBILITY_FILTERS.map(([v,l]) => (
                    <button key={v} className={`afd-chip${templateVisibility===v?' afd-chip--active':''}`} onClick={() => setTemplateVisibility(v)}>{l}</button>
                ))}
            </div>

            {filteredTemplates.length === 0 ? (
                <TemplatesEmptyState filtered={!!search || templateVisibility!=='all'} onReset={() => { setSearch(''); setTemplateVisibility('all'); }} />
            ) : (
                <div className="afd-template-grid">
                    {filteredTemplates.map((item, i) => (
                        <AgentCard key={item.id} className={`agent-template-card animate-slide-in-up stagger-${(i%5)+1}`} agent={item} isPreset onClick={() => onPreviewTemplate && onPreviewTemplate(item)} />
                    ))}
                </div>
            )}

            {showFactoryChat && <AgentFactoryChat onClose={() => setShowFactoryChat(false)} onDeployed={() => { setShowFactoryChat(false); loadAgents().catch(() => {}); }} />}
            <TriggerModal open={!!triggerAgent} onClose={() => setTriggerAgent(null)} targetKind="agent" targetId={triggerAgent?.id} targetName={triggerAgent?.name} />
        </div>
    );
}

export default AgentsDashboard;
