// SPDX-License-Identifier: Apache-2.0
import { useEffect, useMemo, useRef, useState } from 'react';
import { API_BASE, buildAuthHeaders } from '../../config/api';

/**
 * CatalogPicker — chip-list editor for an agent node's attached tools or skills.
 *
 * Props:
 *   kind        – "tools" | "skills"  (drives which endpoint we hit)
 *   attached    – array of slim entries already attached to the agent node
 *                 (e.g. [{name, description, ...}, ...])
 *   onChange    – called with the new attached array whenever the user
 *                 adds or removes an entry
 *
 * Behaviour:
 *   - Fetches GET /{kind}-catalog on mount.
 *   - "Add" dropdown lists catalog entries NOT already attached.
 *   - "Generate new" toggles an inline form (name + optional description)
 *     that POSTs to /{kind}-catalog/generate and auto-attaches the result.
 *
 * Persistence: this component is pure UI. It does NOT save anywhere itself;
 * the parent ConfigPanel mirrors the attached array onto node.data.{tools|skills}
 * via useWorkflowStore().updateNodeData, which is how all other node fields
 * are saved.
 */

const LABELS = {
    tools: {
        title: 'Tools',
        addLabel: 'Add tool',
        generateLabel: 'Generate new tool',
        placeholderName: 'e.g. fetch_weather',
        placeholderDesc: 'Optional: 1-2 sentence description of what the tool does',
        emptyHint: 'No tools attached. Add one from the catalog.',
        emptyCatalogHint: 'Catalog is empty',
        // Tool generation flow isn't wired through this picker, so the
        // "Generate new tool" button is suppressed. Skills are generated via
        // POST /skills-catalog/generate → SkillFactory pipeline.
        allowGenerate: false,
        chipClass: 'agent-tag agent-tag--tool',
    },
    skills: {
        title: 'Skills',
        addLabel: 'Add skill',
        generateLabel: 'Generate new skill',
        placeholderName: 'e.g. sentiment_analysis',
        placeholderDesc: 'Optional: 1-2 sentence description of what the skill teaches',
        emptyHint: 'No skills attached. Add one from the catalog (use the Skills tab to create new).',
        emptyCatalogHint: 'No approved skills yet — create one from the Skills tab',
        // Skill creation lives on the Skills tab only. Agent-side surfaces
        // (this picker) intentionally cannot generate skills — they'd bypass
        // the approval workflow.
        allowGenerate: false,
        chipClass: 'agent-tag agent-tag--skill',
    },
};

/**
 * @param {{ kind: 'tools' | 'skills', attached: Array<{name: string}>, onChange: (next: Array) => void }} props
 */
export default function CatalogPicker({ kind, attached = [], onChange }) {
    const labels = LABELS[kind];
    const pickerRef = useRef(null);
    const searchRef = useRef(null);
    const itemKey = kind; // 'tools' or 'skills' — matches API response shape

    const [catalog, setCatalog] = useState([]);
    const [loadingCatalog, setLoadingCatalog] = useState(false);
    const [catalogError, setCatalogError] = useState('');
    const [pickerOpen, setPickerOpen] = useState(false);
    const [search, setSearch] = useState('');
    const [pendingNames, setPendingNames] = useState([]);

    // Inline generate form
    const [showGenerate, setShowGenerate] = useState(false);
    const [genName, setGenName] = useState('');
    const [genDesc, setGenDesc] = useState('');
    const [generating, setGenerating] = useState(false);
    const [generateError, setGenerateError] = useState('');

    useEffect(() => {
        let cancelled = false;
        const load = async () => {
            setLoadingCatalog(true);
            setCatalogError('');
            try {
                const res = await fetch(`${API_BASE}/${kind}-catalog`, {
                    headers: buildAuthHeaders(),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (!cancelled) setCatalog(data[itemKey] || []);
            } catch (err) {
                if (!cancelled) setCatalogError(err.message);
            } finally {
                if (!cancelled) setLoadingCatalog(false);
            }
        };
        load();
        return () => { cancelled = true; };
    }, [kind, itemKey]);

    useEffect(() => {
        if (!pickerOpen) return undefined;

        const handlePointerDown = (event) => {
            if (!pickerRef.current?.contains(event.target)) {
                setPickerOpen(false);
                setSearch('');
                setPendingNames([]);
            }
        };

        const handleKeyDown = (event) => {
            if (event.key === 'Escape') {
                setPickerOpen(false);
                setSearch('');
                setPendingNames([]);
            }
        };

        document.addEventListener('pointerdown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);

        return () => {
            document.removeEventListener('pointerdown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [pickerOpen]);

    const attachedNames = new Set(attached.map(a => a?.name).filter(Boolean));
    // Only offer skills that are actually usable (approved / live). Owner-visible
    // pending skills appear on the Skills tab but are not attachable to agents.
    // Tools have no is_usable gate; only enforce on skills.
    const available = catalog.filter(c => {
        if (attachedNames.has(c.name)) return false;
        if (kind === 'skills' && c.is_usable === false) return false;
        return true;
    });

    const filteredAvailable = useMemo(() => {
        const q = search.trim().toLowerCase();
        if (!q) return available;
        return available.filter(c =>
            c.name.toLowerCase().includes(q) ||
            (c.description || '').toLowerCase().includes(q)
        );
    }, [available, search]);

    const handleTogglePending = (name) => {
        if (!name) return;
        setPendingNames(prev => (
            prev.includes(name)
                ? prev.filter(n => n !== name)
                : [...prev, name]
        ));
    };

    const handleAddSelected = () => {
        if (pendingNames.length === 0) return;
        const selected = pendingNames
            .map(name => catalog.find(c => c.name === name))
            .filter(Boolean);
        if (selected.length === 0) return;
        onChange([...attached, ...selected]);
        setPickerOpen(false);
        setSearch('');
        setPendingNames([]);
    };

    const handleRemove = (name) => {
        onChange(attached.filter(a => a.name !== name));
    };

    const handleGenerate = async () => {
        const name = genName.trim();
        if (!name || generating) return;
        setGenerating(true);
        setGenerateError('');
        try {
            const res = await fetch(`${API_BASE}/${kind}-catalog/generate`, {
                method: 'POST',
                headers: buildAuthHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ name, description: genDesc.trim() }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
            // Add to attached AND the local catalog cache (so it shows up in
            // the dropdown for any future agent nodes during this session).
            setCatalog(prev => {
                const exists = prev.some(c => c.name === data.name);
                return exists ? prev.map(c => c.name === data.name ? data : c) : [...prev, data];
            });
            onChange([...attached, data]);
            setGenName('');
            setGenDesc('');
            setShowGenerate(false);
        } catch (err) {
            setGenerateError(err.message);
        } finally {
            setGenerating(false);
        }
    };

    return (
        <div className="catalog-picker">
            <div className="catalog-picker__header">
                <label className="form-label">{labels.title}</label>
                {!showGenerate && labels.allowGenerate && (
                    <button
                        type="button"
                        className="catalog-picker__generate-btn"
                        onClick={() => setShowGenerate(true)}
                        title={labels.generateLabel}
                    >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                            <line x1="12" y1="5" x2="12" y2="19" />
                            <line x1="5" y1="12" x2="19" y2="12" />
                        </svg>
                        {labels.generateLabel}
                    </button>
                )}
            </div>

            {/* Chip strip — currently attached items */}
            <div className="catalog-picker__chips">
                {attached.length === 0 && !showGenerate && (
                    <span className="catalog-picker__empty">{labels.emptyHint}</span>
                )}
                {attached.map((entry) => (
                    <span key={entry.name} className={labels.chipClass} title={entry.description || ''}>
                        {entry.name}
                        <button
                            type="button"
                            className="catalog-picker__chip-remove"
                            onClick={() => handleRemove(entry.name)}
                            aria-label={`Remove ${entry.name}`}
                        >
                            <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
                                <line x1="18" y1="6" x2="6" y2="18" />
                                <line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                        </button>
                    </span>
                ))}
            </div>

            {/* Inline "generate new" form */}
            {showGenerate && (
                <div className="catalog-picker__generate-form">
                    <input
                        type="text"
                        className="form-input"
                        value={genName}
                        onChange={(e) => setGenName(e.target.value)}
                        placeholder={labels.placeholderName}
                        autoFocus
                        disabled={generating}
                    />
                    <input
                        type="text"
                        className="form-input"
                        value={genDesc}
                        onChange={(e) => setGenDesc(e.target.value)}
                        placeholder={labels.placeholderDesc}
                        disabled={generating}
                    />
                    {generateError && (
                        <div className="catalog-picker__error">{generateError}</div>
                    )}
                    <div className="catalog-picker__generate-actions">
                        <button
                            type="button"
                            className="modal-btn modal-btn-cancel"
                            onClick={() => { setShowGenerate(false); setGenerateError(''); }}
                            disabled={generating}
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            className="modal-btn modal-btn-generate"
                            onClick={handleGenerate}
                            disabled={!genName.trim() || generating}
                        >
                            {generating ? 'Generating...' : 'Generate'}
                        </button>
                    </div>
                </div>
            )}

            {/* Add-from-catalog picker */}
            {!showGenerate && (
                <div ref={pickerRef} className={`catalog-picker__add ${pickerOpen ? 'catalog-picker__add--open' : ''}`}>
                    <button
                        type="button"
                        className="catalog-picker__select-btn"
                        onClick={() => {
                            setPickerOpen((open) => {
                                if (open) setPendingNames([]);
                                return !open;
                            });
                            setSearch('');
                        }}
                        disabled={loadingCatalog || available.length === 0}
                        aria-expanded={pickerOpen}
                        aria-label={labels.addLabel}
                    >
                        <span>
                            {loadingCatalog
                                ? 'Loading catalog...'
                                : available.length === 0
                                    ? (catalog.length === 0
                                        ? labels.emptyCatalogHint
                                        : 'All catalog entries already attached')
                                    : labels.addLabel + '...'}
                        </span>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M6 9l6 6 6-6" />
                        </svg>
                    </button>
                    {pickerOpen && available.length > 0 && (
                        <div className="catalog-picker__menu" role="listbox" aria-label={labels.addLabel}>
                            <div className="catalog-picker__search-wrap">
                                <svg className="catalog-picker__search-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                    <circle cx="11" cy="11" r="8" />
                                    <line x1="21" y1="21" x2="16.65" y2="16.65" />
                                </svg>
                                <input
                                    ref={searchRef}
                                    className="catalog-picker__search"
                                    type="text"
                                    placeholder={`Search ${kind}...`}
                                    value={search}
                                    onChange={(e) => setSearch(e.target.value)}
                                    autoFocus
                                />
                                {search && (
                                    <button
                                        type="button"
                                        className="catalog-picker__search-clear"
                                        onClick={() => { setSearch(''); searchRef.current?.focus(); }}
                                        aria-label="Clear search"
                                    >
                                        <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
                                            <line x1="18" y1="6" x2="6" y2="18" />
                                            <line x1="6" y1="6" x2="18" y2="18" />
                                        </svg>
                                    </button>
                                )}
                            </div>
                            {filteredAvailable.length > 0 ? (
                                <>
                                    <div className="catalog-picker__bulk-actions">
                                        <span>{pendingNames.length} selected</span>
                                        <button
                                            type="button"
                                            className="catalog-picker__add-selected"
                                            onClick={handleAddSelected}
                                            disabled={pendingNames.length === 0}
                                        >
                                            Add selected
                                        </button>
                                    </div>
                                    {filteredAvailable.map((entry) => {
                                        const checked = pendingNames.includes(entry.name);
                                        return (
                                            <button
                                                key={entry.name}
                                                type="button"
                                                className={`catalog-picker__menu-item ${checked ? 'catalog-picker__menu-item--checked' : ''}`}
                                                onClick={() => handleTogglePending(entry.name)}
                                                role="option"
                                                aria-selected={checked}
                                            >
                                                <span className="catalog-picker__menu-row">
                                                    <span className={`catalog-picker__checkbox ${checked ? 'catalog-picker__checkbox--checked' : ''}`} aria-hidden="true">
                                                        {checked && (
                                                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                                                                <polyline points="20 6 9 17 4 12" />
                                                            </svg>
                                                        )}
                                                    </span>
                                                    <span className="catalog-picker__menu-copy">
                                                        <span className="catalog-picker__menu-name">{entry.name}</span>
                                                        {entry.description && (
                                                            <span className="catalog-picker__menu-desc">{entry.description}</span>
                                                        )}
                                                    </span>
                                                </span>
                                            </button>
                                        );
                                    })}
                                </>
                            ) : (
                                <div className="catalog-picker__no-results">
                                    No matching {kind} found
                                </div>
                            )}
                        </div>
                    )}
                    {catalogError && (
                        <div className="catalog-picker__error">Failed to load catalog: {catalogError}</div>
                    )}
                </div>
            )}
        </div>
    );
}
