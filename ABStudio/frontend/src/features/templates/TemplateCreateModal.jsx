// SPDX-License-Identifier: Apache-2.0
// Create-template modal for the optional template editor feature.
// Mounted from WorkflowsDashboard.jsx alongside TemplateEditModal when the
// backend feature flag is on. Talks to `templateAdminStore.createTemplate`,
// which POSTs to `/template-admin` and persists the new row to BOTH the
// `templates` table AND the seed overrides sidecar so it survives a DB
// wipe.
import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTriggerPortalContainer } from '../triggers/triggerPortal';
import useTemplateAdminStore from '../../store/templateAdminStore';
import { CATEGORY_OPTIONS, DEFAULT_CATEGORY } from '../workflows/templateCategories';

// Mirrors the minimum viable graph used by `handleCreateNew` in
// WorkflowsDashboard so a freshly-seeded template opens cleanly in the
// graph editor for further customisation.
const DEFAULT_GRAPH = {
    nodes: [
        { id: 'start', type: 'start', position: { x: 100, y: 200 }, data: { label: 'Start' } },
        {
            id: 'agent',
            type: 'agent',
            position: { x: 320, y: 200 },
            data: {
                name: 'New Agent',
                instructions: '',
                provider: 'custom',
                apiKey: '',
                modelName: '',
                temperature: 0.7,
                maxTokens: 2048,
                topP: 1.0,
                baseUrl: '',
            },
        },
        { id: 'end', type: 'end', position: { x: 540, y: 200 }, data: { label: 'End' } },
    ],
    edges: [
        { id: 'e1', source: 'start', target: 'agent' },
        { id: 'e2', source: 'agent', target: 'end' },
    ],
};

// Convert a freeform name into a stable `template-…` id. Strips
// non-alphanumerics, collapses runs of dashes, and lower-cases.
function slugify(name) {
    const base = (name || '')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '');
    return base ? `template-${base}` : '';
}

function TemplateCreateModal({ open, existingIds, onCreated, onClose }) {
    const createTemplate = useTemplateAdminStore((s) => s.createTemplate);
    const error = useTemplateAdminStore((s) => s.error);
    const clearError = useTemplateAdminStore((s) => s.clearError);

    // Portal container carries `data-ac` so the build-time PostCSS prefix
    // (every selector is scoped to `[data-ac]`) matches and the modal picks
    // up the standard light-theme styles even though it lives outside the
    // Build Studio root.
    const portalContainer = useTriggerPortalContainer();

    const [name, setName] = useState('');
    // `manualId === null` means "auto-derive from name"; any string (incl. "")
    // means the user has taken control and we stop syncing.
    const [manualId, setManualId] = useState(null);
    const [description, setDescription] = useState('');
    const [category, setCategory] = useState(DEFAULT_CATEGORY);
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (!open) return;
        setName('');
        setManualId(null);
        setDescription('');
        setCategory(DEFAULT_CATEGORY);
        clearError();
    }, [open, clearError]);

    // O(1) collision lookup; existingIds is a stable prop courtesy of the
    // parent's useMemo.
    const existingIdSet = useMemo(
        () => new Set(Array.isArray(existingIds) ? existingIds : []),
        [existingIds],
    );

    const id = manualId ?? slugify(name);

    if (!open) return null;

    const trimmedId = id.trim();
    const trimmedName = name.trim();
    const idCollides = !!trimmedId && existingIdSet.has(trimmedId);
    const canSave = !saving && !!trimmedName && !!trimmedId && !idCollides;

    const handleSave = async () => {
        if (!canSave) return;
        setSaving(true);
        const created = await createTemplate({
            id: trimmedId,
            name: trimmedName,
            description: description.trim(),
            category: category || DEFAULT_CATEGORY,
            // Pattern + HITL are derived from the graph the user actually
            // builds in the editor; defaulting here keeps the schema happy
            // without surfacing meaningless choices at create-time.
            pattern: 'sequential',
            hitl: false,
            graphData: DEFAULT_GRAPH,
        });
        setSaving(false);
        if (created) {
            onCreated && onCreated(created);
            onClose && onClose();
        }
    };

    const handleClose = () => {
        if (saving) return;
        onClose && onClose();
    };

    if (!portalContainer) return null;

    // The dashboard wrapper has an `animate-fade-in` transform, which
    // makes it the containing block for any `position: fixed` descendant
    // (per the CSS spec). That pushes our overlay off-screen on shorter
    // viewports because the dashboard is taller than the window. Portalling
    // out to a `data-ac` root under <body> sidesteps the transform ancestor
    // entirely so `fixed` resolves against the actual viewport, AND keeps
    // the build-time `[data-ac]` CSS scoping intact so the modal stays
    // styled.
    return createPortal(
        <div className="confirm-modal-overlay" onClick={handleClose}>
            <div
                className="confirm-modal template-create-modal"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="confirm-modal-header template-create-modal__header">
                    <div>
                        <h3 className="confirm-modal-title">Create new template</h3>
                        <p className="template-create-modal__subtitle">
                            Add a new entry to the workflow catalog. A starter graph is created automatically.
                        </p>
                    </div>
                    <button className="confirm-modal-close" onClick={handleClose} aria-label="Close">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M18 6L6 18M6 6l12 12" />
                        </svg>
                    </button>
                </div>

                <div className="template-create-modal__body">
                    <label className="template-create-modal__field">
                        <span className="template-create-modal__label">Name</span>
                        <input
                            type="text"
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            disabled={saving}
                            placeholder="e.g. Quarterly Risk Report"
                            className="template-create-modal__input"
                            autoFocus
                        />
                    </label>

                    <label className="template-create-modal__field">
                        <span className="template-create-modal__label">Template id</span>
                        <input
                            type="text"
                            value={id}
                            onChange={(e) => setManualId(e.target.value)}
                            disabled={saving}
                            placeholder="template-quarterly-risk-report"
                            className="template-create-modal__input"
                            aria-invalid={idCollides || undefined}
                        />
                        {idCollides && (
                            <span className="template-create-modal__hint template-create-modal__hint--error">
                                That id is already taken — pick another.
                            </span>
                        )}
                    </label>

                    <label className="template-create-modal__field">
                        <span className="template-create-modal__label">Description</span>
                        <textarea
                            rows={3}
                            value={description}
                            onChange={(e) => setDescription(e.target.value)}
                            disabled={saving}
                            placeholder="What does this workflow do?"
                            className="template-create-modal__input template-create-modal__textarea"
                        />
                    </label>

                    <label className="template-create-modal__field">
                        <span className="template-create-modal__label">Category</span>
                        <select
                            value={category}
                            onChange={(e) => setCategory(e.target.value)}
                            disabled={saving}
                            className="template-create-modal__input"
                        >
                            {CATEGORY_OPTIONS.map((c) => <option key={c} value={c}>{c}</option>)}
                        </select>
                    </label>

                    {error && (
                        <div className="template-create-modal__error" role="alert">
                            {error}
                        </div>
                    )}
                </div>

                <div className="confirm-modal-actions template-create-modal__actions">
                    <button className="confirm-modal-btn cancel" onClick={handleClose} disabled={saving}>
                        Cancel
                    </button>
                    <button
                        className="confirm-modal-btn confirm"
                        onClick={handleSave}
                        disabled={!canSave}
                    >
                        {saving ? 'Creating…' : 'Create template'}
                    </button>
                </div>
            </div>
        </div>,
        portalContainer,
    );
}

export default TemplateCreateModal;
