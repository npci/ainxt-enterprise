// SPDX-License-Identifier: MIT
import { useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '../../../config/api';

/**
 * SubflowPicker — searchable custom dropdown for picking an existing saved
 * agent or workflow to link into the current canvas.
 *
 * Modes:
 *   single — node config: callers store one { kind, refId, refName }.
 *   multi  — agent editor: callers store an array of those objects.
 *
 * Props:
 *   value             current selection (object for single, array for multi)
 *   onChange          (next) => void
 *   mode              'single' | 'multi'
 *   excludeWorkflowId hides the open workflow from the list to prevent
 *                     self-reference at design time.
 */
function SubflowPicker({
    value,
    onChange,
    mode = 'single',
    excludeWorkflowId = '',
}) {
    const [agents, setAgents] = useState([]);
    const [workflows, setWorkflows] = useState([]);
    const [agentTemplates, setAgentTemplates] = useState([]);
    const [workflowTemplates, setWorkflowTemplates] = useState([]);
    const [status, setStatus] = useState('loading'); // loading | loaded | error
    const [error, setError] = useState('');
    // Lightweight in-flight flag so the "Use" button shows a spinner state.
    const [instantiating, setInstantiating] = useState(false);

    const [open, setOpen] = useState(false);
    const [search, setSearch] = useState('');
    const [activeIdx, setActiveIdx] = useState(0);
    // 'all' | 'agent' | 'workflow' | 'template' — lets the user scope the
    // list down to just one kind of asset.
    const [filterKind, setFilterKind] = useState('all');
    // Per-section collapse state. A section is collapsed when its id is in
    // this Set; its option rows are skipped from the rendered list. Section
    // ids are stable strings (see ``items`` builder below) so the toggle
    // persists across re-renders while the popover stays open.
    const [collapsed, setCollapsed] = useState(() => new Set());

    const wrapRef = useRef(null);
    const searchRef = useRef(null);
    const listRef = useRef(null);

    /* ── Load catalogs ──────────────────────────────────────────────── */
    const reloadCatalogs = async () => {
        const [a, w, at, wt] = await Promise.all([
            apiFetch('/agents').catch(() => []),
            apiFetch('/workflows').catch(() => []),
            apiFetch('/agent-templates').catch(() => []),
            apiFetch('/templates').catch(() => []),
        ]);
        setAgents(Array.isArray(a) ? a : []);
        setWorkflows(Array.isArray(w) ? w : []);
        setAgentTemplates(Array.isArray(at) ? at : []);
        setWorkflowTemplates(Array.isArray(wt) ? wt : []);
    };

    useEffect(() => {
        let cancelled = false;
        (async () => {
            setStatus('loading');
            try {
                await reloadCatalogs();
                if (cancelled) return;
                setStatus('loaded');
            } catch (err) {
                if (cancelled) return;
                setError(err.message || 'Failed to load assets');
                setStatus('error');
            }
        })();
        return () => { cancelled = true; };
    }, []);

    /* ── Close on outside click ─────────────────────────────────────── */
    useEffect(() => {
        if (!open) return undefined;
        const onClick = (e) => {
            if (!wrapRef.current?.contains(e.target)) setOpen(false);
        };
        const onEsc = (e) => { if (e.key === 'Escape') setOpen(false); };
        document.addEventListener('mousedown', onClick);
        document.addEventListener('keydown', onEsc);
        return () => {
            document.removeEventListener('mousedown', onClick);
            document.removeEventListener('keydown', onEsc);
        };
    }, [open]);

    /* ── Autofocus search when opened ───────────────────────────────── */
    useEffect(() => {
        if (open) {
            setSearch('');
            setActiveIdx(0);
            setFilterKind('all');
            setCollapsed(new Set());
            requestAnimationFrame(() => searchRef.current?.focus());
        }
    }, [open]);

    const toggleSection = (sectionId) => {
        setCollapsed((prev) => {
            const next = new Set(prev);
            if (next.has(sectionId)) next.delete(sectionId);
            else next.add(sectionId);
            return next;
        });
        // Reset the keyboard cursor since the visible row count just changed.
        setActiveIdx(0);
    };

    /* ── Filtered + flattened option list ────────────────────────────── */
    const items = useMemo(() => {
        const q = search.trim().toLowerCase();
        const matches = (entity) => {
            const n = (entity.name || '').toLowerCase();
            const d = (entity.description || '').toLowerCase();
            return !q || n.includes(q) || d.includes(q);
        };
        const filteredAgents          = agents.filter(matches);
        const filteredWorkflows       = workflows
            .filter((w) => excludeWorkflowId ? w.id !== excludeWorkflowId : true)
            .filter(matches);
        const filteredAgentTemplates    = agentTemplates.filter(matches);
        const filteredWorkflowTemplates = workflowTemplates.filter(matches);

        // For multi-mode, hide already-attached items
        const attachedSet = mode === 'multi' && Array.isArray(value)
            ? new Set(value.map((v) => `${v.kind}:${v.refId}`))
            : null;
        const result = [];

        const showAgents     = filterKind === 'all'      || filterKind === 'agent';
        const showWorkflows  = filterKind === 'all'      || filterKind === 'workflow';
        const showTemplates  = filterKind === 'all'      || filterKind === 'template';

        // Each section emits exactly one header (with a stable sectionId).
        // Its option rows are appended only when the section is expanded.
        const pushSection = (sectionId, label, icon, rows, buildOpt) => {
            if (!rows.length) return;
            result.push({
                kind: 'header',
                sectionId,
                label,
                icon,
                count: rows.length,
                collapsed: collapsed.has(sectionId),
            });
            if (collapsed.has(sectionId)) return;
            for (const r of rows) {
                const opt = buildOpt(r);
                if (!opt) continue;
                result.push(opt);
            }
        };

        if (showAgents) {
            pushSection('agents', 'Agents', 'agent', filteredAgents, (a) => {
                if (attachedSet && attachedSet.has(`agent:${a.id}`)) return null;
                return {
                    kind: 'option', assetKind: 'agent', id: a.id,
                    name: a.name || a.id, description: a.description || '',
                };
            });
        }
        if (showWorkflows) {
            pushSection('workflows', 'Workflows', 'workflow', filteredWorkflows, (w) => {
                if (attachedSet && attachedSet.has(`workflow:${w.id}`)) return null;
                return {
                    kind: 'option', assetKind: 'workflow', id: w.id,
                    name: w.name || w.id, description: w.description || '',
                };
            });
        }
        // Templates render last so they're discoverable but don't push the
        // user's own assets below the fold.
        if (showTemplates) {
            pushSection('agent-templates', 'Agent templates', 'agent', filteredAgentTemplates, (t) => ({
                kind: 'option', assetKind: 'agent-template', id: t.id,
                name: t.name || t.id, description: t.description || '',
            }));
            pushSection('workflow-templates', 'Workflow templates', 'workflow', filteredWorkflowTemplates, (t) => ({
                kind: 'option', assetKind: 'workflow-template', id: t.id,
                name: t.name || t.id, description: t.description || '',
            }));
        }
        return result;
    }, [
        agents, workflows, agentTemplates, workflowTemplates,
        search, excludeWorkflowId, mode, value, filterKind, collapsed,
    ]);

    // Indexes of selectable options (skip headers) for keyboard navigation.
    const optionIndexes = useMemo(
        () => items.map((it, i) => (it.kind === 'option' ? i : -1)).filter((i) => i >= 0),
        [items],
    );

    const clampedActive = optionIndexes.length === 0
        ? 0
        : optionIndexes[Math.min(Math.max(0, activeIdx), optionIndexes.length - 1)];

    /* ── Selection commit ───────────────────────────────────────────── */
    // Templates are not directly runnable — they're presets. Selecting one
    // creates a real instance under the user's namespace (POST .../use) and
    // links the new id, so subflow execution can dispatch to it like any
    // other saved agent/workflow.
    const instantiateTemplate = async (item) => {
        try {
            setInstantiating(true);
            if (item.assetKind === 'agent-template') {
                const created = await apiFetch(`/agent-templates/${encodeURIComponent(item.id)}/use`, { method: 'POST' });
                await reloadCatalogs();
                return { kind: 'agent', refId: created.id, refName: created.name || item.name };
            }
            if (item.assetKind === 'workflow-template') {
                const created = await apiFetch(`/templates/${encodeURIComponent(item.id)}/use`, { method: 'POST' });
                await reloadCatalogs();
                return { kind: 'workflow', refId: created.id, refName: created.name || item.name };
            }
            return null;
        } catch (err) {
            setError(err.message || 'Failed to instantiate template');
            return null;
        } finally {
            setInstantiating(false);
        }
    };

    const commit = async (item) => {
        if (!item || item.kind !== 'option') return;

        let next;
        if (item.assetKind === 'agent-template' || item.assetKind === 'workflow-template') {
            const instantiated = await instantiateTemplate(item);
            if (!instantiated) return;
            next = instantiated;
        } else {
            next = { kind: item.assetKind, refId: item.id, refName: item.name };
        }

        if (mode === 'single') {
            onChange(next);
            setOpen(false);
        } else {
            const list = Array.isArray(value) ? [...value] : [];
            const exists = list.some((v) => v.kind === next.kind && v.refId === next.refId);
            if (!exists) list.push(next);
            onChange(list);
            // Keep open so the user can attach multiple in one session.
            setSearch('');
            setActiveIdx(0);
            searchRef.current?.focus();
        }
    };

    /* ── Keyboard navigation ────────────────────────────────────────── */
    const handleKeyDown = (e) => {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            setActiveIdx((i) => Math.min(i + 1, Math.max(0, optionIndexes.length - 1)));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActiveIdx((i) => Math.max(i - 1, 0));
        } else if (e.key === 'Enter') {
            e.preventDefault();
            commit(items[clampedActive]);
        }
    };

    /* ── Scroll active option into view ─────────────────────────────── */
    useEffect(() => {
        if (!open) return;
        const el = listRef.current?.querySelector('[data-active="true"]');
        if (el && typeof el.scrollIntoView === 'function') {
            el.scrollIntoView({ block: 'nearest' });
        }
    }, [clampedActive, open]);

    /* ── Render helpers ─────────────────────────────────────────────── */
    const renderTrigger = () => {
        if (mode === 'single') {
            const has = value && value.refId;
            return (
                <button
                    type="button"
                    className={`subflow-trigger${open ? ' subflow-trigger--open' : ''}${has ? ' subflow-trigger--selected' : ''}`}
                    onClick={() => setOpen((v) => !v)}
                >
                    {has ? (
                        <span className="subflow-trigger-content">
                            <span className={`subflow-pill subflow-pill--${value.kind || 'agent'}`}>
                                {value.kind === 'workflow' ? 'Workflow' : 'Agent'}
                            </span>
                            <span className="subflow-trigger-name">{value.refName || value.refId}</span>
                        </span>
                    ) : (
                        <span className="subflow-trigger-placeholder">
                            Choose an existing workflow or agent…
                        </span>
                    )}
                    <svg
                        className="subflow-trigger-caret"
                        width="14" height="14" viewBox="0 0 24 24" fill="none"
                        stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"
                    >
                        <path d="M6 9l6 6 6-6" />
                    </svg>
                </button>
            );
        }
        return (
            <button
                type="button"
                className={`subflow-trigger subflow-trigger--multi${open ? ' subflow-trigger--open' : ''}`}
                onClick={() => setOpen((v) => !v)}
            >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="12" y1="5" x2="12" y2="19" />
                    <line x1="5" y1="12" x2="19" y2="12" />
                </svg>
                <span className="subflow-trigger-placeholder">Attach a workflow or agent</span>
            </button>
        );
    };

    const renderItemRow = (it, flatIdx) => {
        if (it.kind === 'header') {
            const isCollapsed = !!it.collapsed;
            return (
                <button
                    key={`h-${it.sectionId}`}
                    type="button"
                    className={`subflow-list-header${isCollapsed ? ' subflow-list-header--collapsed' : ''}`}
                    onClick={() => toggleSection(it.sectionId)}
                    aria-expanded={!isCollapsed}
                    aria-controls={`subflow-section-${it.sectionId}`}
                    title={isCollapsed ? `Expand ${it.label}` : `Collapse ${it.label}`}
                >
                    <span className="subflow-list-header-chevron" aria-hidden="true">
                        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M6 9l6 6 6-6" />
                        </svg>
                    </span>
                    <span className="subflow-list-header-icon">
                        {it.icon === 'agent' ? (
                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M22 2L11 13" />
                                <path d="M22 2L15 22L11 13L2 9L22 2Z" />
                            </svg>
                        ) : (
                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <rect x="3" y="3" width="8" height="8" rx="1.5" />
                                <rect x="13" y="3" width="8" height="8" rx="1.5" />
                                <rect x="3" y="13" width="8" height="8" rx="1.5" />
                                <rect x="13" y="13" width="8" height="8" rx="1.5" />
                            </svg>
                        )}
                    </span>
                    <span className="subflow-list-header-label">{it.label}</span>
                    <span className="subflow-list-header-count">{it.count}</span>
                </button>
            );
        }
        const active = flatIdx === clampedActive;
        const isTemplate = it.assetKind === 'agent-template' || it.assetKind === 'workflow-template';
        const baseKind   = isTemplate
            ? (it.assetKind === 'agent-template' ? 'agent' : 'workflow')
            : it.assetKind;
        const pillLabel = isTemplate
            ? (baseKind === 'workflow' ? 'Workflow template' : 'Agent template')
            : (baseKind === 'workflow' ? 'Workflow' : 'Agent');
        return (
            <button
                key={`${it.assetKind}-${it.id}`}
                type="button"
                role="option"
                aria-selected={active}
                data-active={active}
                className={`subflow-option${active ? ' subflow-option--active' : ''}`}
                onMouseEnter={() => {
                    const idxInOptionList = optionIndexes.indexOf(flatIdx);
                    if (idxInOptionList >= 0) setActiveIdx(idxInOptionList);
                }}
                onClick={() => commit(it)}
                disabled={instantiating && isTemplate}
            >
                <span className={`subflow-option-icon subflow-option-icon--${baseKind}${isTemplate ? ' subflow-option-icon--template' : ''}`}>
                    {baseKind === 'agent' ? (
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M22 2L11 13" />
                            <path d="M22 2L15 22L11 13L2 9L22 2Z" />
                        </svg>
                    ) : (
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                            <rect x="3" y="3" width="8" height="8" rx="1.5" />
                            <rect x="13" y="3" width="8" height="8" rx="1.5" />
                            <rect x="3" y="13" width="8" height="8" rx="1.5" />
                            <rect x="13" y="13" width="8" height="8" rx="1.5" />
                        </svg>
                    )}
                </span>
                <span className="subflow-option-text">
                    <span className="subflow-option-name">
                        {it.name}
                        {isTemplate && (
                            <svg
                                width="11" height="11" viewBox="0 0 24 24" fill="none"
                                stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"
                                style={{ marginLeft: 6, verticalAlign: '-1px', color: '#94a3b8' }}
                                aria-label="Template"
                            >
                                <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
                            </svg>
                        )}
                    </span>
                    {it.description && (
                        <span className="subflow-option-desc" title={it.description}>
                            {it.description}
                        </span>
                    )}
                </span>
                <span className={`subflow-pill subflow-pill--${baseKind}${isTemplate ? ' subflow-pill--template' : ''}`}>
                    {pillLabel}
                </span>
            </button>
        );
    };

    const renderAttachedChips = () => {
        if (mode !== 'multi') return null;
        const list = Array.isArray(value) ? value : [];
        if (list.length === 0) {
            return (
                <p className="subflow-chips-empty">
                    No workflows or agents attached yet.
                </p>
            );
        }
        return (
            <div className="subflow-chips">
                {list.map((item, idx) => (
                    <span
                        key={`${item.kind}:${item.refId}:${idx}`}
                        className={`subflow-chip subflow-chip--${item.kind}`}
                    >
                        <span className={`subflow-pill subflow-pill--${item.kind}`}>
                            {item.kind === 'workflow' ? 'Workflow' : 'Agent'}
                        </span>
                        <span className="subflow-chip-name">{item.refName || item.refId}</span>
                        <button
                            type="button"
                            className="subflow-chip-remove"
                            title="Remove"
                            onClick={() => onChange(list.filter((_, i) => i !== idx))}
                            aria-label={`Remove ${item.refName || item.refId}`}
                        >
                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round">
                                <line x1="18" y1="6" x2="6" y2="18" />
                                <line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                        </button>
                    </span>
                ))}
            </div>
        );
    };

    return (
        <div className="subflow-picker" ref={wrapRef}>
            {renderTrigger()}

            {open && (
                <div className="subflow-popover" role="listbox">
                    <div className="subflow-search">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                            <circle cx="11" cy="11" r="8" />
                            <line x1="21" y1="21" x2="16.65" y2="16.65" />
                        </svg>
                        <input
                            ref={searchRef}
                            type="text"
                            placeholder="Search workflows and agents…"
                            value={search}
                            onChange={(e) => { setSearch(e.target.value); setActiveIdx(0); }}
                            onKeyDown={handleKeyDown}
                        />
                        {search && (
                            <button
                                type="button"
                                className="subflow-search-clear"
                                onClick={() => { setSearch(''); searchRef.current?.focus(); }}
                                title="Clear search"
                            >
                                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round">
                                    <line x1="18" y1="6" x2="6" y2="18" />
                                    <line x1="6" y1="6" x2="18" y2="18" />
                                </svg>
                            </button>
                        )}
                    </div>

                    {/* Kind filter — scope the list. Defaults to "All" so the
                        existing search / keyboard behaviour is unchanged when
                        the user doesn't touch it. */}
                    <div className="subflow-filter" role="tablist" aria-label="Filter by kind">
                        {[
                            { id: 'all', label: 'All' },
                            { id: 'agent', label: 'Agents' },
                            { id: 'workflow', label: 'Workflows' },
                            { id: 'template', label: 'Templates' },
                        ].map((tab) => (
                            <button
                                key={tab.id}
                                type="button"
                                role="tab"
                                aria-selected={filterKind === tab.id}
                                className={`subflow-filter-tab${filterKind === tab.id ? ' subflow-filter-tab--active' : ''}`}
                                onClick={() => {
                                    setFilterKind(tab.id);
                                    setActiveIdx(0);
                                    searchRef.current?.focus();
                                }}
                            >
                                {tab.label}
                            </button>
                        ))}
                    </div>

                    <div className="subflow-list" ref={listRef}>
                        {status === 'loading' && (
                            <div className="subflow-empty">Loading…</div>
                        )}
                        {instantiating && (
                            <div className="subflow-empty">Creating from template…</div>
                        )}
                        {status === 'error' && (
                            <div className="subflow-empty subflow-empty--error">{error || 'Failed to load.'}</div>
                        )}
                        {status === 'loaded' && items.length === 0 && (
                            <div className="subflow-empty">
                                {search ? `No matches for “${search}”.` : 'No workflows or agents yet.'}
                            </div>
                        )}
                        {status === 'loaded' && items.map((it, i) => renderItemRow(it, i))}
                    </div>

                    <div className="subflow-footer">
                        <kbd>↑ ↓</kbd> navigate <kbd>↵</kbd> select <kbd>esc</kbd> close
                    </div>
                </div>
            )}

            {mode === 'single' && status === 'error' && !open && (
                <span className="form-hint" style={{ color: '#dc2626' }}>{error}</span>
            )}

            {renderAttachedChips()}
        </div>
    );
}

export default SubflowPicker;
