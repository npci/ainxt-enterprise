// SPDX-License-Identifier: Apache-2.0
// Edit-metadata modal for the optional template editor feature.
// Mounted from WorkflowsDashboard.jsx when `useTemplateAdminStore` reports
// the backend feature flag is on. To remove the feature, delete this file
// along with the rest of `src/features/templates/`.
import { useEffect, useState } from 'react';
import useTemplateAdminStore from '../../store/templateAdminStore';
import { CATEGORY_OPTIONS, DEFAULT_CATEGORY } from '../workflows/templateCategories';

const PATTERN_OPTIONS = [
    'sequential',
    'parallel',
    'conditional',
    'loop',
    'loop_conditional',
    'parallel_conditional',
];

function TemplateEditModal({ open, template, onSaved, onClose }) {
    const updateTemplate = useTemplateAdminStore((s) => s.updateTemplate);
    const error = useTemplateAdminStore((s) => s.error);
    const clearError = useTemplateAdminStore((s) => s.clearError);

    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [category, setCategory] = useState(DEFAULT_CATEGORY);
    const [pattern, setPattern] = useState('sequential');
    const [hitl, setHitl] = useState(false);
    const [saving, setSaving] = useState(false);

    // Reset form fields whenever the modal opens for a different template.
    useEffect(() => {
        if (!open || !template) return;
        setName(template.name || '');
        setDescription(template.description || '');
        // Fall back to the default if the stored category predates the
        // fixed taxonomy (e.g. a stale free-text value).
        setCategory(CATEGORY_OPTIONS.includes(template.category) ? template.category : DEFAULT_CATEGORY);
        setPattern(template.pattern || 'sequential');
        setHitl(!!template.hitl);
        clearError();
    }, [open, template, clearError]);

    if (!open || !template) return null;

    const handleSave = async () => {
        setSaving(true);
        const updated = await updateTemplate(template.id, {
            name: name.trim(),
            description: description.trim(),
            category: category.trim(),
            pattern,
            hitl,
        });
        setSaving(false);
        if (updated) {
            onSaved && onSaved(updated);
            onClose && onClose();
        }
    };

    const handleClose = () => {
        if (saving) return;
        onClose && onClose();
    };

    return (
        <div className="confirm-modal-overlay" onClick={handleClose}>
            <div
                className="confirm-modal"
                style={{ maxWidth: 540, width: '92%' }}
                onClick={(e) => e.stopPropagation()}
            >
                <div className="confirm-modal-header">
                    <h3 className="confirm-modal-title">Edit template</h3>
                    <button className="confirm-modal-close" onClick={handleClose} aria-label="Close">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M18 6L6 18M6 6l12 12" />
                        </svg>
                    </button>
                </div>

                <div style={{ display: 'grid', gap: 12, padding: '4px 0 8px' }}>
                    <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                        <span style={{ color: '#475569', fontWeight: 600 }}>Name</span>
                        <input
                            type="text"
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            disabled={saving}
                            style={inputStyle}
                        />
                    </label>

                    <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                        <span style={{ color: '#475569', fontWeight: 600 }}>Description</span>
                        <textarea
                            rows={3}
                            value={description}
                            onChange={(e) => setDescription(e.target.value)}
                            disabled={saving}
                            style={{ ...inputStyle, resize: 'vertical', minHeight: 72 }}
                        />
                    </label>

                    <div style={{ display: 'grid', gap: 12, gridTemplateColumns: '1fr 1fr' }}>
                        <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                            <span style={{ color: '#475569', fontWeight: 600 }}>Category</span>
                            <select
                                value={category}
                                onChange={(e) => setCategory(e.target.value)}
                                disabled={saving}
                                style={inputStyle}
                            >
                                {CATEGORY_OPTIONS.map((c) => <option key={c} value={c}>{c}</option>)}
                            </select>
                        </label>

                        <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                            <span style={{ color: '#475569', fontWeight: 600 }}>Pattern</span>
                            <select
                                value={pattern}
                                onChange={(e) => setPattern(e.target.value)}
                                disabled={saving}
                                style={inputStyle}
                            >
                                {PATTERN_OPTIONS.map((p) => (
                                    <option key={p} value={p}>{p}</option>
                                ))}
                            </select>
                        </label>
                    </div>

                    <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                        <input
                            type="checkbox"
                            checked={hitl}
                            onChange={(e) => setHitl(e.target.checked)}
                            disabled={saving}
                        />
                        <span style={{ color: '#475569' }}>
                            Human-in-the-loop required (badge only — actual gates are set per-agent)
                        </span>
                    </label>

                    {error && (
                        <div style={{
                            color: '#b91c1c',
                            background: '#fef2f2',
                            border: '1px solid #fecaca',
                            padding: '6px 10px',
                            borderRadius: 6,
                            fontSize: 12,
                        }}>
                            {error}
                        </div>
                    )}
                </div>

                <div className="confirm-modal-actions">
                    <button className="confirm-modal-btn cancel" onClick={handleClose} disabled={saving}>
                        Cancel
                    </button>
                    <button
                        className="confirm-modal-btn primary"
                        onClick={handleSave}
                        disabled={saving || !name.trim()}
                    >
                        {saving ? 'Saving…' : 'Save changes'}
                    </button>
                </div>
            </div>
        </div>
    );
}

const inputStyle = {
    border: '1px solid #cbd5e1',
    borderRadius: 6,
    padding: '6px 10px',
    fontSize: 13,
    background: '#fff',
    outline: 'none',
    fontFamily: 'inherit',
};

export default TemplateEditModal;
