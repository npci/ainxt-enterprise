// SPDX-License-Identifier: Apache-2.0
import { useEffect, useMemo, useRef, useState } from 'react';
import { API_BASE, buildAuthHeaders } from '../../config/api';

/**
 * InlinePicker — a self-contained tools/skills picker styled with inline
 * styles so it never depends on the global CSS cascade. Used by the
 * Workflow Factory and Agent Factory chat overlays where shared
 * CatalogPicker class styles weren't reliably applying.
 *
 * Props:
 *   kind:     'tools' | 'skills'
 *   attached: array of {name, description, ...} entries currently attached
 *   onChange: (next) => void   called whenever the attached set changes
 */

export const PICKER_LABELS = {
    tools: {
        title: 'Tools',
        addLabel: 'Add tool',
        generateLabel: 'Generate new tool',
        placeholderName: 'e.g. fetch_weather',
        placeholderDesc: 'Optional: 1-2 sentence description',
        emptyHint: 'No tools attached. Add from catalog.',
        emptyCatalogHint: 'Catalog is empty',
        // Tool generation flow isn't wired through this picker, so the
        // "Generate new tool" button is suppressed. Skills are generated via
        // POST /skills-catalog/generate → SkillFactory pipeline.
        allowGenerate: false,
        chipBg: '#eef2ff', chipBorder: '#c7d2fe', chipColor: '#4338ca',
    },
    skills: {
        title: 'Skills',
        addLabel: 'Add skill',
        generateLabel: 'Generate new skill',
        placeholderName: 'e.g. sentiment_analysis',
        placeholderDesc: 'Optional: 1-2 sentence description',
        emptyHint: 'No skills attached. Add from catalog (create new via the Skills tab).',
        emptyCatalogHint: 'No approved skills yet — create one from the Skills tab',
        // Agent-side surfaces cannot generate skills — that would bypass
        // the approval workflow. Creation lives on the Skills tab.
        allowGenerate: false,
        chipBg: '#ecfeff', chipBorder: '#a5f3fc', chipColor: '#0e7490',
    },
};

export default function InlinePicker({ kind, attached = [], onChange }) {
    const labels = PICKER_LABELS[kind];
    const [catalog, setCatalog] = useState([]);
    const [loadingCatalog, setLoadingCatalog] = useState(false);
    const [catalogError, setCatalogError] = useState('');
    const [pickerOpen, setPickerOpen] = useState(false);
    const [search, setSearch] = useState('');

    const [showGenerate, setShowGenerate] = useState(false);
    const [genName, setGenName] = useState('');
    const [genDesc, setGenDesc] = useState('');
    const [generating, setGenerating] = useState(false);
    const [generateError, setGenerateError] = useState('');

    const pickerRef = useRef(null);

    useEffect(() => {
        let cancelled = false;
        const load = async () => {
            setLoadingCatalog(true);
            setCatalogError('');
            try {
                const res = await fetch(`${API_BASE}/${kind}-catalog`, { headers: buildAuthHeaders() });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                if (!cancelled) setCatalog(data[kind] || []);
            } catch (err) {
                if (!cancelled) setCatalogError(err.message);
            } finally {
                if (!cancelled) setLoadingCatalog(false);
            }
        };
        load();
        return () => { cancelled = true; };
    }, [kind]);

    useEffect(() => {
        if (!pickerOpen) return undefined;
        const onDown = (e) => {
            if (!pickerRef.current?.contains(e.target)) {
                setPickerOpen(false);
                setSearch('');
            }
        };
        const onKey = (e) => { if (e.key === 'Escape') { setPickerOpen(false); setSearch(''); } };
        document.addEventListener('pointerdown', onDown);
        document.addEventListener('keydown', onKey);
        return () => {
            document.removeEventListener('pointerdown', onDown);
            document.removeEventListener('keydown', onKey);
        };
    }, [pickerOpen]);

    const attachedNames = new Set(attached.map(a => a?.name).filter(Boolean));
    // Skills that haven't been approved yet are shown on the Skills tab but
    // must not be attachable to agents. Tools have no gating.
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

    const handleAdd = (name) => {
        const entry = catalog.find(c => c.name === name);
        if (!entry) return;
        onChange([...attached, entry]);
        setPickerOpen(false);
        setSearch('');
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
            setCatalog(prev => {
                const exists = prev.some(c => c.name === data.name);
                return exists ? prev.map(c => c.name === data.name ? data : c) : [...prev, data];
            });
            onChange([...attached, data]);
            setGenName(''); setGenDesc(''); setShowGenerate(false);
        } catch (err) {
            setGenerateError(err.message);
        } finally {
            setGenerating(false);
        }
    };

    const styles = PICKER_STYLES;
    const disabled = loadingCatalog || available.length === 0;

    return (
        <div style={styles.root}>
            <div style={styles.header}>
                <span style={styles.label}>{labels.title}</span>
                {!showGenerate && labels.allowGenerate && (
                    <button
                        type="button"
                        style={styles.generateBtn}
                        onClick={() => setShowGenerate(true)}
                    >
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                            <line x1="12" y1="5" x2="12" y2="19" />
                            <line x1="5" y1="12" x2="19" y2="12" />
                        </svg>
                        {labels.generateLabel}
                    </button>
                )}
            </div>

            <div style={styles.chips}>
                {attached.length === 0 && !showGenerate && (
                    <span style={styles.empty}>{labels.emptyHint}</span>
                )}
                {attached.map((entry) => (
                    <span
                        key={entry.name}
                        title={entry.description || ''}
                        style={{
                            ...styles.chip,
                            background: labels.chipBg,
                            borderColor: labels.chipBorder,
                            color: labels.chipColor,
                        }}
                    >
                        {entry.name}
                        <button
                            type="button"
                            style={styles.chipRemove}
                            onClick={() => handleRemove(entry.name)}
                            aria-label={`Remove ${entry.name}`}
                        >
                            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
                                <line x1="18" y1="6" x2="6" y2="18" />
                                <line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                        </button>
                    </span>
                ))}
            </div>

            {showGenerate && (
                <div style={styles.generateForm}>
                    <input
                        type="text"
                        style={styles.input}
                        value={genName}
                        onChange={(e) => setGenName(e.target.value)}
                        placeholder={labels.placeholderName}
                        autoFocus
                        disabled={generating}
                    />
                    <input
                        type="text"
                        style={styles.input}
                        value={genDesc}
                        onChange={(e) => setGenDesc(e.target.value)}
                        placeholder={labels.placeholderDesc}
                        disabled={generating}
                    />
                    {generateError && <div style={styles.error}>{generateError}</div>}
                    <div style={styles.generateActions}>
                        <button
                            type="button"
                            style={styles.cancelBtn}
                            onClick={() => { setShowGenerate(false); setGenerateError(''); }}
                            disabled={generating}
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            style={styles.confirmBtn(!genName.trim() || generating)}
                            onClick={handleGenerate}
                            disabled={!genName.trim() || generating}
                        >
                            {generating ? 'Generating…' : 'Generate'}
                        </button>
                    </div>
                </div>
            )}

            {!showGenerate && (
                <div ref={pickerRef} style={{ position: 'relative' }}>
                    <button
                        type="button"
                        style={styles.selectBtn(disabled)}
                        onClick={() => { setPickerOpen(o => !o); setSearch(''); }}
                        disabled={disabled}
                    >
                        <span style={styles.selectBtnText}>
                            {loadingCatalog
                                ? 'Loading catalog…'
                                : available.length === 0
                                    ? (catalog.length === 0
                                        ? labels.emptyCatalogHint
                                        : 'All catalog entries already attached')
                                    : `${labels.addLabel}…`}
                        </span>
                        <svg
                            width="13" height="13" viewBox="0 0 24 24" fill="none"
                            stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"
                            style={{
                                color: '#4f46e5',
                                transform: pickerOpen ? 'rotate(180deg)' : 'rotate(0)',
                                transition: 'transform 160ms ease',
                                flexShrink: 0,
                            }}
                        >
                            <path d="M6 9l6 6 6-6" />
                        </svg>
                    </button>
                    {pickerOpen && available.length > 0 && (
                        <div style={styles.menu}>
                            <div style={styles.searchWrap}>
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                                    <circle cx="11" cy="11" r="8" />
                                    <line x1="21" y1="21" x2="16.65" y2="16.65" />
                                </svg>
                                <input
                                    style={styles.searchInput}
                                    type="text"
                                    placeholder={`Search ${kind}…`}
                                    value={search}
                                    onChange={(e) => setSearch(e.target.value)}
                                    autoFocus
                                />
                            </div>
                            {filteredAvailable.length > 0 ? (
                                filteredAvailable.map((entry) => (
                                    <button
                                        key={entry.name}
                                        type="button"
                                        style={styles.menuItem}
                                        onClick={() => handleAdd(entry.name)}
                                    >
                                        <span style={styles.menuItemName}>{entry.name}</span>
                                        {entry.description && (
                                            <span style={styles.menuItemDesc}>{entry.description}</span>
                                        )}
                                    </button>
                                ))
                            ) : (
                                <div style={styles.noResults}>No matching {kind} found</div>
                            )}
                        </div>
                    )}
                    {catalogError && <div style={styles.error}>Failed to load: {catalogError}</div>}
                </div>
            )}
        </div>
    );
}

export const PICKER_STYLES = {
    root: {
        display: 'flex', flexDirection: 'column', gap: '6px',
    },
    header: {
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px',
    },
    label: {
        fontSize: '10.5px', fontWeight: 700,
        color: '#475569',
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
    },
    generateBtn: {
        display: 'inline-flex', alignItems: 'center', gap: '4px',
        padding: '4px 10px',
        fontSize: '11px', fontWeight: 600,
        color: '#4f46e5',
        background: '#eef2ff',
        border: '1px solid #c7d2fe',
        borderRadius: '8px',
        cursor: 'pointer',
        fontFamily: 'inherit',
    },
    chips: {
        display: 'flex', flexWrap: 'wrap', gap: '6px',
        minHeight: '22px', alignItems: 'center',
    },
    empty: {
        fontSize: '11.5px', color: '#94a3b8', fontStyle: 'italic',
    },
    chip: {
        display: 'inline-flex', alignItems: 'center', gap: '4px',
        padding: '3px 4px 3px 10px',
        fontSize: '11.5px', fontWeight: 600,
        borderRadius: '999px',
        border: '1px solid',
        lineHeight: 1.4,
        whiteSpace: 'nowrap',
    },
    chipRemove: {
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        width: '14px', height: '14px',
        background: 'rgba(15,23,42,0.08)',
        border: 'none', borderRadius: '50%',
        color: 'currentColor', cursor: 'pointer',
        padding: 0,
    },
    selectBtn: (disabled) => ({
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px',
        width: '100%', padding: '8px 12px',
        background: disabled ? '#f1f5f9' : '#ffffff',
        border: '1px solid #e2e8f0',
        borderRadius: '10px',
        fontSize: '12.5px', color: '#475569',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.7 : 1,
        fontFamily: 'inherit',
        textAlign: 'left',
    }),
    selectBtnText: {
        flex: 1, minWidth: 0,
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
    },
    menu: {
        position: 'absolute',
        top: 'calc(100% + 4px)', left: 0, right: 0,
        zIndex: 1100,
        background: '#fff',
        border: '1px solid #e2e8f0',
        borderRadius: '12px',
        boxShadow: '0 12px 32px rgba(15,23,42,0.14)',
        maxHeight: '240px',
        overflowY: 'auto',
        padding: '4px',
    },
    searchWrap: {
        display: 'flex', alignItems: 'center', gap: '6px',
        padding: '6px 8px',
        position: 'sticky', top: 0,
        background: '#fff',
        borderBottom: '1px solid #eef2f7',
        marginBottom: '2px',
    },
    searchInput: {
        flex: 1, minWidth: 0,
        border: 'none', outline: 'none', background: 'transparent',
        fontSize: '12.5px', color: '#0f172a',
        padding: '2px 0',
        fontFamily: 'inherit',
    },
    menuItem: {
        display: 'flex', flexDirection: 'column', gap: '2px',
        width: '100%',
        padding: '8px 10px',
        background: 'transparent',
        border: 'none', borderRadius: '8px',
        textAlign: 'left', cursor: 'pointer',
        fontFamily: 'inherit',
    },
    menuItemName: {
        fontSize: '12.5px', fontWeight: 600, color: '#0f172a',
    },
    menuItemDesc: {
        fontSize: '11.5px', color: '#64748b', lineHeight: 1.4,
    },
    noResults: {
        padding: '12px 10px', textAlign: 'center',
        fontSize: '12px', color: '#94a3b8',
    },
    generateForm: {
        display: 'flex', flexDirection: 'column', gap: '8px',
        padding: '10px',
        background: '#f8fafc',
        border: '1px solid #e2e8f0',
        borderRadius: '8px',
    },
    input: {
        width: '100%',
        padding: '8px 10px',
        background: '#fff',
        border: '1px solid #e2e8f0',
        borderRadius: '8px',
        fontSize: '12.5px', color: '#0f172a',
        outline: 'none',
        fontFamily: 'inherit',
        boxSizing: 'border-box',
    },
    error: {
        fontSize: '11px', color: '#b91c1c',
        padding: '4px 6px',
        background: '#fef2f2',
        border: '1px solid #fecaca',
        borderRadius: '4px',
    },
    generateActions: {
        display: 'flex', gap: '8px', justifyContent: 'flex-end',
    },
    cancelBtn: {
        padding: '7px 14px',
        background: 'transparent',
        border: '1px solid #e2e8f0',
        borderRadius: '8px',
        color: '#475569',
        fontSize: '12px', fontWeight: 600,
        cursor: 'pointer',
        fontFamily: 'inherit',
    },
    confirmBtn: (disabled) => ({
        padding: '7px 14px',
        background: disabled ? '#c7d2fe' : 'linear-gradient(135deg, #4f46e5, #7c3aed)',
        border: 'none',
        borderRadius: '8px',
        color: '#fff',
        fontSize: '12px', fontWeight: 600,
        cursor: disabled ? 'not-allowed' : 'pointer',
        fontFamily: 'inherit',
    }),
};
