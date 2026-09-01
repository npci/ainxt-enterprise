// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useCallback, useRef } from 'react';
import { API_BASE, buildAuthHeaders } from '../../config/api';
import HoverTooltip from '../../components/common/HoverTooltip';
import useHoverTooltip from '../../hooks/useHoverTooltip';
import useCurrentUser from '../../hooks/useCurrentUser';
import SkillFactoryChat from './SkillFactoryChat';
import StatusBadge from '../governance/StatusBadge';
import SubmitApprovalButton from '../governance/SubmitApprovalButton';
import formatDate from '../../utils/formatDate';

// Known platform-shipped skill names. Used only as a fallback for legacy rows
// that predate the `generated` column being set reliably. The authoritative
// signal is the backend `generated` flag: skills seeded from the platform
// folder are stored with generated=false, while anything created via the
// Skill Factory is stored with generated=true.
const BUILTIN_SKILL_NAMES = new Set([
    'doc-coauthoring',
    'docx',
    'pdf',
    'pptx',
    'xlsx',
]);

// Classify a skill by its origin so the Skills tab can filter / badge each
// one distinctly. Three sources exist:
//
//   - ``builtin`` — shipped with the platform (seeded by the backend from the
//                   AiNxt skills folder or the canonical set).
//   - ``ai``      — created via the Skill Factory (AI-generated).
//   - ``upload``  — imported by a user from a packaged .zip / .skill bundle.
//
// The authoritative signal is the backend ``source`` field (added post-launch;
// see workflow_repo.upsert_skill and the ALTER in the skills_catalog init).
// For legacy rows that predate the column we fall back to ``generated`` and
// then to the built-in name list.
function getSkillSource(skill) {
    if (skill && typeof skill.source === 'string') {
        const s = skill.source.toLowerCase();
        if (s === 'builtin' || s === 'ai' || s === 'upload') return s;
    }
    if (skill && typeof skill.generated === 'boolean') {
        return skill.generated ? 'ai' : 'builtin';
    }
    return BUILTIN_SKILL_NAMES.has(skill?.name) ? 'builtin' : 'ai';
}

// AI-generated and uploaded skills share the same governance lifecycle
// (created_by / status / approver / edit / delete / submit-for-approval).
// Built-in / seeded skills are exempt. Centralise the predicate so the
// card, meta row, and detail modal stay in sync.
function isGovernedSource(src) {
    return src === 'ai' || src === 'upload';
}

// Cap the chip row so a small catalog with lots of one-skill categories
// doesn't drown the UI in pills. Anything past the cap can still be reached
// via search.
const CATEGORY_CHIP_LIMIT = 5;

// Categories are derived from the catalog at render time so we only ever
// show buckets that have at least one skill. Returns the top N by count.
function deriveCategories(allSkills) {
    const counts = new Map();
    for (const s of allSkills) {
        const cat = (s.category || 'general').toLowerCase();
        counts.set(cat, (counts.get(cat) || 0) + 1);
    }
    const sorted = Array.from(counts.entries())
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .map(([key, count]) => ({
            key,
            // Replace underscores with spaces and Title-case each word so
            // "text_processing" → "Text Processing".
            label: key
                .split(/[_\s]+/)
                .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
                .join(' '),
            count,
        }));
    return {
        top: sorted.slice(0, CATEGORY_CHIP_LIMIT),
        hiddenCount: Math.max(0, sorted.length - CATEGORY_CHIP_LIMIT),
        all: sorted,
    };
}

// ── Badges ──────────────────────────────────────────────────────────────────

function SourceBadge({ source }) {
    const labels = {
        builtin: 'Built-in',
        ai:      'AI Generated',
        upload:  'Uploaded',
    };
    const label = labels[source];
    if (!label) return null;
    return <span className={`skill-badge skill-badge-${source}`}>{label}</span>;
}

// Renders the reviewer-requested metadata row: scope, creator, approver, dates.
// Uses only the fields the backend already returns on the enriched
// GET /skills-catalog response; hidden entirely for built-ins where these
// fields are meaningless.
function SkillMeta({ skill }) {
    if (skill.generated === false) return null;
    const scope = skill.visibility === 'public'
        ? 'Public'
        : (skill.visibility === 'private' ? `Department${skill.department ? `: ${skill.department}` : ''}` : null);
    const createdBy = skill.created_by_name || skill.created_by_email || skill.created_by;
    const approvedBy = skill.approved_by_name || skill.approved_by_email || skill.approved_by;
    // Even before approval, surface a placeholder so the owner sees the
    // full lifecycle at a glance (status → who will approve later).
    const pending = String(skill.status || '').toUpperCase().startsWith('PENDING');
    return (
        <div className="skill-card-meta" style={{
            marginTop: 8, display: 'flex', flexDirection: 'column', gap: 2,
            fontSize: 11, color: '#64748b', lineHeight: 1.5,
        }}>
            {scope && <div><b style={{ color: '#475569' }}>Scope:</b> {scope}</div>}
            {createdBy && (
                <div>
                    <b style={{ color: '#475569' }}>Created by:</b> {createdBy}
                    {skill.created_at && <> · {formatDate(skill.created_at)}</>}
                </div>
            )}
            {approvedBy && skill.approved_at ? (
                <div>
                    <b style={{ color: '#475569' }}>Approved by:</b> {approvedBy} · {formatDate(skill.approved_at)}
                </div>
            ) : (pending && (
                <div>
                    <b style={{ color: '#475569' }}>Approved by:</b>{' '}
                    <span style={{ fontStyle: 'italic', color: '#94a3b8' }}>Awaiting approval</span>
                </div>
            ))}
        </div>
    );
}

// ── Detail modal ─────────────────────────────────────────────────────────────

function SkillDetailModal({ skill, onClose, onDelete, onUpdated }) {
    // The list snapshot can lag behind the governance mirror (submit is
    // fire-and-forget), so we always re-fetch the detail on open and prefer
    // the fresh row for meta rendering. Falls back to the passed-in ``skill``
    // until the fetch resolves so the header doesn't flash blank.
    const [detail, setDetail] = useState(skill);
    const displaySkill = { ...skill, ...detail };
    const source = getSkillSource(displaySkill);
    const me = useCurrentUser();
    const isAdmin = me.role === 'admin';
    const [content, setContent] = useState('');
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [deleting, setDeleting] = useState(false);
    // Edit mode: allowed only when the current user is the creator AND the
    // skill is in a state that makes sense to edit (draft / pending / rejected).
    // Approved / live skills stay read-only from this modal — changing them
    // demotes to DRAFT server-side and would surprise the user. Admins may
    // edit any AI-generated skill (matches backend policy in catalog.py).
    const EDITABLE_STATUSES = new Set([null, undefined, 'DRAFT', 'PENDING_APPROVAL', 'REJECTED']);
    const canEdit = isGovernedSource(source)
        && (!!displaySkill.is_owner || isAdmin)
        && EDITABLE_STATUSES.has(displaySkill.status);
    // Delete: creator any time; admin any time; nobody else.
    const canDelete = isGovernedSource(source) && (!!displaySkill.is_owner || isAdmin);
    const [isEditing, setIsEditing] = useState(false);
    const [draft, setDraft] = useState('');
    const [saving, setSaving] = useState(false);
    const bodyRef = useRef(null);

    useEffect(() => {
        bodyRef.current?.scrollTo({ top: 0 });
        const load = async () => {
            try {
                const res = await fetch(`${API_BASE}/skills-catalog/${encodeURIComponent(skill.name)}`, {
                    headers: buildAuthHeaders(),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                setContent(data.content || '');
                setDetail(data);
                // Push the fresh row up so the underlying card also refreshes
                // its meta (scope / creator / status) after the modal opens.
                onUpdated?.(data);
            } catch (e) {
                setError(e.message);
            } finally {
                setLoading(false);
            }
        };
        load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [skill.name]);

    const handleDelete = async () => {
        if (!window.confirm(`Delete skill "${skill.name}"?`)) return;
        setDeleting(true);
        try {
            const res = await fetch(`${API_BASE}/skills-catalog/${encodeURIComponent(skill.name)}`, {
                method: 'DELETE', headers: buildAuthHeaders(),
            });
            if (!res.ok && res.status !== 404) throw new Error(`HTTP ${res.status}`);
            onDelete(skill.name);
            onClose();
        } catch (e) {
            setError(e.message);
            setDeleting(false);
        }
    };

    const handleSave = async () => {
        if (saving) return;
        setSaving(true);
        setError('');
        try {
            const res = await fetch(`${API_BASE}/skills-catalog`, {
                method: 'POST',
                headers: buildAuthHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({
                    name: skill.name,
                    content: draft,
                    description: skill.description || '',
                    category: skill.category || 'general',
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
            setContent(draft);
            setIsEditing(false);
            onUpdated?.(data);
        } catch (e) {
            setError(e.message);
        } finally {
            setSaving(false);
        }
    };

    // Escape closes the modal; lock body scroll while open so the page behind
    // doesn't drift around when the user scroll-wheels over the backdrop.
    useEffect(() => {
        const onKey = (e) => { if (e.key === 'Escape') onClose(); };
        window.addEventListener('keydown', onKey);
        const prevOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        return () => {
            window.removeEventListener('keydown', onKey);
            document.body.style.overflow = prevOverflow;
        };
    }, [onClose]);

    return (
        <div className="skill-modal-overlay" onClick={onClose}>
            <div className="skill-modal" onClick={(e) => e.stopPropagation()}>
                <header className="skill-modal-header">
                    <div className="skill-modal-heading">
                        <div className="skill-modal-title-row">
                            <h2 className="skill-modal-title">{displaySkill.name}</h2>
                            <SourceBadge source={source} />
                        </div>
                        <span className="skill-modal-category">{displaySkill.category || 'general'}</span>
                        {displaySkill.description && (
                            <p className="skill-modal-description">{displaySkill.description}</p>
                        )}
                        <SkillMeta skill={displaySkill} />
                    </div>
                    <div className="skill-modal-actions">
                        {canEdit && !isEditing && (
                            <button
                                type="button"
                                className="skill-modal-delete"
                                style={{ background: '#eef2ff', color: '#4338ca', borderColor: '#c7d2fe' }}
                                onClick={() => { setDraft(content); setIsEditing(true); }}
                                disabled={loading}
                                title="Edit this skill's SKILL.md — it stays in the same approval state."
                            >
                                Edit
                            </button>
                        )}
                        {canDelete && !isEditing && (
                            <button
                                type="button"
                                className="skill-modal-delete"
                                onClick={handleDelete}
                                disabled={deleting}
                            >
                                {deleting ? 'Deleting…' : 'Delete'}
                            </button>
                        )}
                        <button
                            type="button"
                            className="skill-modal-close"
                            onClick={onClose}
                            aria-label="Close"
                        >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                <line x1="18" y1="6" x2="6" y2="18" />
                                <line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                        </button>
                    </div>
                </header>

                <div ref={bodyRef} className="skill-modal-body">
                    {error && <div className="skill-modal-error">{error}</div>}
                    {loading ? (
                        <div className="skill-modal-loading">Loading…</div>
                    ) : isEditing ? (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                            <textarea
                                value={draft}
                                onChange={(e) => setDraft(e.target.value)}
                                spellCheck={false}
                                style={{
                                    width: '100%', minHeight: 360, boxSizing: 'border-box',
                                    padding: 12, fontFamily: 'ui-monospace,SFMono-Regular,Menlo,monospace',
                                    fontSize: 12, lineHeight: 1.55, borderRadius: 8,
                                    border: '1px solid #e2e8f0', outline: 'none', resize: 'vertical',
                                }}
                            />
                            <div style={{ fontSize: 11, color: '#64748b' }}>
                                Saving keeps the skill in its current approval state. Approvers will see the latest content on their next review.
                            </div>
                            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                                <button
                                    type="button"
                                    className="skills-create-btn"
                                    style={{ background: '#fff', color: '#475569', border: '1px solid #e2e8f0' }}
                                    onClick={() => { setIsEditing(false); setDraft(''); setError(''); }}
                                    disabled={saving}
                                >
                                    Cancel
                                </button>
                                <button
                                    type="button"
                                    className="skills-create-btn"
                                    onClick={handleSave}
                                    disabled={saving || draft === content}
                                >
                                    {saving ? 'Saving…' : 'Save'}
                                </button>
                            </div>
                        </div>
                    ) : (
                        <pre className="skill-modal-content">{content || '(no content)'}</pre>
                    )}
                </div>
            </div>
        </div>
    );
}

// ── Upload modal ──────────────────────────────────────────────────────────────

/** Human-readable file size (KB / MB / GB). */
function _fmtBytes(n) {
    if (!Number.isFinite(n) || n < 0) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/** Peek into a zip and read the ``name`` from SKILL.md's YAML frontmatter so we
 *  can show the skill name in the picker BEFORE the user hits Upload. Fully
 *  client-side, best-effort — returns "" on any failure. Uses the browser's
 *  built-in DecompressionStream so no dependency is added. */
async function _peekSkillName(file) {
    try {
        // Only .zip / .skill archives are supported by the upload endpoint,
        // and both use the PK ZIP format. If DecompressionStream isn't there
        // (older browsers) we give up silently.
        if (typeof DecompressionStream === 'undefined') return '';
        const buf = new Uint8Array(await file.arrayBuffer());
        // Find "SKILL.md" in the central directory as a marker, then locally
        // parse the corresponding local-file-header. This is a pragmatic
        // shortcut: full zip parsing isn't worth the code for a preview.
        const text = new TextDecoder('latin1').decode(buf);
        // Grab the first "---\n...name: <value>...---" block that follows any
        // "SKILL.md" reference. We can't reliably decompress a single entry
        // without a real zip parser, so we search the raw bytes for the
        // frontmatter directly — SKILL.md is very often stored uncompressed
        // (STORE method) for archives created by ainxt's skill CLI.
        const nameMatch = text.match(/\bname:\s*([^\r\n]+)/);
        if (nameMatch && nameMatch[1]) {
            return nameMatch[1].trim().replace(/^['"]|['"]$/g, '');
        }
        return '';
    } catch {
        return '';
    }
}

function SkillUploadModal({ onClose, onUploaded }) {
    const [file, setFile] = useState(null);
    const [previewName, setPreviewName] = useState('');    // parsed from SKILL.md
    const [visibility, setVisibility] = useState('private');
    const [category, setCategory] = useState('');
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState('');
    const [success, setSuccess] = useState(null);          // { name, visibility, bundle_files_written, bundle_files_error }
    const [dragOver, setDragOver] = useState(false);
    const fileInputRef = useRef(null);

    useEffect(() => {
        const onKey = (e) => { if (e.key === 'Escape') onClose(); };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [onClose]);

    // When a file is chosen, try to preview the skill name from its SKILL.md.
    useEffect(() => {
        let cancelled = false;
        if (!file) { setPreviewName(''); return; }
        _peekSkillName(file).then((name) => { if (!cancelled) setPreviewName(name); });
        return () => { cancelled = true; };
    }, [file]);

    const _acceptFile = (f) => {
        if (!f) return;
        const nameLower = (f.name || '').toLowerCase();
        if (!nameLower.endsWith('.zip') && !nameLower.endsWith('.skill')) {
            setError('Only .zip or .skill archives are supported.');
            return;
        }
        setError('');
        setFile(f);
    };

    const handleUpload = async () => {
        if (!file || uploading) return;
        setUploading(true);
        setError('');
        setSuccess(null);
        try {
            const form = new FormData();
            form.append('file', file);
            form.append('visibility', visibility);
            if (category.trim()) form.append('category', category.trim());
            const res = await fetch(`${API_BASE}/skills-catalog/upload`, {
                method: 'POST',
                // omit JSON content-type so the browser sets the multipart boundary
                headers: buildAuthHeaders({ omitContentType: true }),
                body: form,
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                // Backend uses HTTPException(detail=...) — surface that as the
                // rejection reason. Fall back to the status line so the user
                // never sees an empty error banner.
                const reason = data?.detail || data?.message || `HTTP ${res.status} ${res.statusText || ''}`.trim();
                throw new Error(reason);
            }
            setSuccess(data);
        } catch (e) {
            setError(e.message || 'Upload failed.');
        } finally {
            setUploading(false);
        }
    };

    // ── Success view ─────────────────────────────────────────────────────
    if (success) {
        const uploadedName = success.name || previewName || file?.name || 'Skill';
        const bundleErr    = success.bundle_files_error || '';
        return (
            <div className="skill-modal-overlay" onClick={onClose}>
                <div className="skill-modal" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
                    <header className="skill-modal-header">
                        <div className="skill-modal-heading">
                            <h2 className="skill-modal-title">Skill uploaded</h2>
                            <p className="skill-modal-description">
                                Submitted for approval. It appears in the catalog after an approver signs off.
                            </p>
                        </div>
                        <div className="skill-modal-actions">
                            <button type="button" className="skill-modal-close" onClick={onClose} aria-label="Close">
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                    <line x1="18" y1="6" x2="6" y2="18" />
                                    <line x1="6" y1="6" x2="18" y2="18" />
                                </svg>
                            </button>
                        </div>
                    </header>

                    <div className="skill-modal-body" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                        <div style={{
                            display: 'flex', alignItems: 'center', gap: 12,
                            padding: '12px 14px', borderRadius: 12,
                            background: '#ecfdf5', border: '1px solid #a7f3d0',
                        }}>
                            <div style={{
                                width: 36, height: 36, flexShrink: 0, borderRadius: 10,
                                background: '#10b981', color: '#fff',
                                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                            }}>
                                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                    <polyline points="20 6 9 17 4 12" />
                                </svg>
                            </div>
                            <div style={{ minWidth: 0 }}>
                                <div style={{ fontSize: 13.5, fontWeight: 700, color: '#065f46', wordBreak: 'break-word' }}>
                                    {uploadedName}
                                </div>
                                <div style={{ fontSize: 12, color: '#047857', marginTop: 2 }}>
                                    Visibility: {success.visibility || visibility} · Pending approval
                                </div>
                            </div>
                        </div>

                        {bundleErr && (
                            <div className="skill-modal-error">
                                Bundle warning: {bundleErr}
                            </div>
                        )}

                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                            <button
                                type="button"
                                className="skills-create-btn"
                                onClick={() => onUploaded(success)}
                            >
                                Done
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    // ── Upload form ──────────────────────────────────────────────────────
    const canUpload = !!file && !uploading;

    const _onBrowse = () => fileInputRef.current?.click();
    const _onDragOver = (e) => { e.preventDefault(); e.stopPropagation(); setDragOver(true); };
    const _onDragLeave = (e) => { e.preventDefault(); e.stopPropagation(); setDragOver(false); };
    const _onDrop = (e) => {
        e.preventDefault(); e.stopPropagation();
        setDragOver(false);
        _acceptFile(e.dataTransfer?.files?.[0]);
    };

    return (
        <div className="skill-modal-overlay" onClick={onClose}>
            <div className="skill-modal" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
                <header className="skill-modal-header">
                    <div className="skill-modal-heading">
                        <h2 className="skill-modal-title">Upload Skill</h2>
                        <p className="skill-modal-description">
                            Upload a packaged bundle (.zip or .skill) with a SKILL.md and optional
                            scripts/references. It is validated then submitted for approval.
                        </p>
                    </div>
                    <div className="skill-modal-actions">
                        <button type="button" className="skill-modal-close" onClick={onClose} aria-label="Close">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                <line x1="18" y1="6" x2="6" y2="18" />
                                <line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                        </button>
                    </div>
                </header>

                <div className="skill-modal-body" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                    {error && (
                        <div className="skill-modal-error" role="alert">
                            <div style={{ fontWeight: 700, marginBottom: 2 }}>Upload rejected</div>
                            <div style={{ fontWeight: 500, wordBreak: 'break-word' }}>{error}</div>
                        </div>
                    )}

                    {/* Dropzone / selected-file card */}
                    <div>
                        <div style={{ fontSize: 12, fontWeight: 700, color: '#334155', marginBottom: 6, letterSpacing: '0.02em', textTransform: 'uppercase' }}>
                            Skill bundle
                        </div>
                        <input
                            ref={fileInputRef}
                            type="file"
                            accept=".zip,.skill"
                            onChange={(e) => _acceptFile(e.target.files?.[0])}
                            style={{ display: 'none' }}
                        />

                        {file ? (
                            <div style={{
                                display: 'flex', alignItems: 'center', gap: 12,
                                padding: '12px 14px', borderRadius: 12,
                                background: '#f8fafc', border: '1px solid #e2e8f0',
                            }}>
                                <div style={{
                                    width: 38, height: 38, flexShrink: 0, borderRadius: 10,
                                    background: 'linear-gradient(135deg, #4f46e5, #7c3aed)', color: '#fff',
                                    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                                }}>
                                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                                        <polyline points="14 2 14 8 20 8" />
                                    </svg>
                                </div>
                                <div style={{ minWidth: 0, flex: 1 }}>
                                    <div style={{ fontSize: 13, fontWeight: 700, color: '#0f172a', wordBreak: 'break-word' }}>
                                        {file.name}
                                    </div>
                                    <div style={{ fontSize: 11.5, color: '#64748b', marginTop: 2 }}>
                                        {_fmtBytes(file.size)}
                                        {previewName ? <> · Skill name: <strong style={{ color: '#334155' }}>{previewName}</strong></> : null}
                                    </div>
                                </div>
                                <button
                                    type="button"
                                    onClick={() => { setFile(null); setPreviewName(''); setError(''); if (fileInputRef.current) fileInputRef.current.value = ''; }}
                                    disabled={uploading}
                                    aria-label="Remove file"
                                    style={{
                                        width: 28, height: 28, flexShrink: 0, borderRadius: 8,
                                        border: '1px solid #e2e8f0', background: '#fff', color: '#64748b',
                                        cursor: uploading ? 'not-allowed' : 'pointer',
                                        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                                    }}
                                >
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                        <line x1="18" y1="6" x2="6" y2="18" />
                                        <line x1="6" y1="6" x2="18" y2="18" />
                                    </svg>
                                </button>
                            </div>
                        ) : (
                            <div
                                role="button"
                                tabIndex={0}
                                onClick={_onBrowse}
                                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _onBrowse(); } }}
                                onDragOver={_onDragOver}
                                onDragEnter={_onDragOver}
                                onDragLeave={_onDragLeave}
                                onDrop={_onDrop}
                                style={{
                                    display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                                    gap: 8, padding: '22px 16px', borderRadius: 12, cursor: 'pointer',
                                    background: dragOver ? '#eef2ff' : '#f8fafc',
                                    border: `1.5px dashed ${dragOver ? '#6366f1' : '#cbd5e1'}`,
                                    transition: 'background 140ms ease, border-color 140ms ease',
                                    textAlign: 'center',
                                }}
                            >
                                <div style={{
                                    width: 42, height: 42, borderRadius: 12,
                                    background: '#eef2ff', color: '#4f46e5',
                                    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                                }}>
                                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                                        <polyline points="17 8 12 3 7 8" />
                                        <line x1="12" y1="3" x2="12" y2="15" />
                                    </svg>
                                </div>
                                <div style={{ fontSize: 13, fontWeight: 700, color: '#0f172a' }}>
                                    Drop a bundle here, or click to browse
                                </div>
                                <div style={{ fontSize: 11.5, color: '#64748b' }}>
                                    Accepts .zip or .skill
                                </div>
                            </div>
                        )}
                    </div>

                    {/* Category */}
                    <div>
                        <div style={{ fontSize: 12, fontWeight: 700, color: '#334155', marginBottom: 6, letterSpacing: '0.02em', textTransform: 'uppercase' }}>
                            Category <span style={{ fontWeight: 500, color: '#94a3b8', textTransform: 'none', letterSpacing: 0 }}>(optional)</span>
                        </div>
                        <input
                            type="text"
                            value={category}
                            placeholder="e.g. data, research, productivity"
                            onChange={(e) => setCategory(e.target.value)}
                            disabled={uploading}
                            style={{
                                display: 'block', width: '100%', boxSizing: 'border-box',
                                padding: '9px 12px', border: '1px solid #e2e8f0', borderRadius: 10,
                                fontSize: 13, color: '#0f172a', background: uploading ? '#f8fafc' : '#fff',
                                outline: 'none',
                            }}
                        />
                    </div>

                    {/* Visibility */}
                    <div>
                        <div style={{ fontSize: 12, fontWeight: 700, color: '#334155', marginBottom: 6, letterSpacing: '0.02em', textTransform: 'uppercase' }}>
                            Visibility
                        </div>
                        <div style={{ display: 'inline-flex', border: '1px solid #e2e8f0', borderRadius: 10, overflow: 'hidden', background: '#fff' }}>
                            {['private', 'public'].map((v) => {
                                const active = visibility === v;
                                return (
                                    <button
                                        key={v}
                                        type="button"
                                        onClick={() => setVisibility(v)}
                                        disabled={uploading}
                                        style={{
                                            padding: '8px 16px', border: 'none', cursor: uploading ? 'not-allowed' : 'pointer',
                                            fontSize: 12.5, fontWeight: 650, textTransform: 'capitalize',
                                            background: active ? 'linear-gradient(135deg, #4f46e5, #7c3aed)' : '#fff',
                                            color: active ? '#fff' : '#475569',
                                            transition: 'background 140ms ease, color 140ms ease',
                                        }}
                                    >
                                        {v}
                                    </button>
                                );
                            })}
                        </div>
                    </div>

                    {/* Actions */}
                    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 4, paddingTop: 10, borderTop: '1px solid #eef2f7' }}>
                        <button
                            type="button"
                            onClick={onClose}
                            disabled={uploading}
                            style={{
                                height: 36, padding: '0 16px', borderRadius: 11,
                                border: '1px solid #e2e8f0', background: '#fff', color: '#475569',
                                fontSize: 13, fontWeight: 650, cursor: uploading ? 'not-allowed' : 'pointer',
                            }}
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            className="skills-create-btn"
                            style={{
                                opacity: canUpload ? 1 : 0.55,
                                cursor: canUpload ? 'pointer' : 'not-allowed',
                                minWidth: 128, justifyContent: 'center',
                            }}
                            onClick={handleUpload}
                            disabled={!canUpload}
                        >
                            {uploading ? (
                                <>
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" style={{ animation: 'spin 0.9s linear infinite' }}>
                                        <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                                    </svg>
                                    Uploading…
                                </>
                            ) : (
                                <>
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                                        <polyline points="17 8 12 3 7 8" />
                                        <line x1="12" y1="3" x2="12" y2="15" />
                                    </svg>
                                    Upload
                                </>
                            )}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}

// ── Skill card ────────────────────────────────────────────────────────────────

function SkillCard({ skill, onClick, onDelete }) {
    const source = getSkillSource(skill);
    const me = useCurrentUser();
    // AI-generated skills can be removed from the catalog directly from the
    // card. Built-in skills don't expose delete here — they're either platform
    // assets or uploaded/imported and managed elsewhere.
    // Only the creator can delete their own skill; admins may delete any
    // AI-generated skill for cleanup / policy enforcement. Other users see
    // no delete affordance so a stranger can't remove your work.
    const canDelete = isGovernedSource(source) && (!!skill.is_owner || (me.role === 'admin'));
    const tooltip = useHoverTooltip({ enabled: !!skill.description });

    const handleKeyDown = (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onClick(skill);
        }
    };

    const handleDeleteClick = (e) => {
        e.stopPropagation();
        if (!window.confirm(`Delete skill "${skill.name}"?`)) return;
        onDelete(skill);
    };

    return (
        <div
            {...tooltip.anchorProps}
            role="button"
            tabIndex={0}
            className="skill-card"
            onClick={() => onClick(skill)}
            onKeyDown={handleKeyDown}
        >
            <div className="skill-card-head">
                <span className="skill-card-name">{skill.name}</span>
                <SourceBadge source={source} />
                {/* Governance approval status — only AI-created skills are
                    governed; built-in/platform skills are exempt. */}
                {isGovernedSource(source) && skill.name && (
                    <StatusBadge entityType="skills" name={skill.name} poll={false} />
                )}
            </div>
            <span className="skill-card-category">{skill.category || 'general'}</span>
            {skill.description && (
                <p className="skill-card-desc">{skill.description}</p>
            )}
            <SkillMeta skill={skill} />
            {/* Submit-for-approval — AI-created skills only, and only shown to
                the creator. A non-owner (admin/HOD viewing a pending skill
                so they can approve it) acts from the Inbox instead; showing
                Deploy/Cancel here would mislead them into thinking they can
                withdraw or resubmit someone else's request. */}
            {isGovernedSource(source) && skill.name && skill.is_owner && (
                // Stop BOTH click AND keyboard events from bubbling into the
                // card. The card treats Space / Enter as "open detail modal"
                // (accessible activation), which used to steal every space
                // the user typed into the Deploy modal's reason textarea and
                // pop the detail view on top of the popover. Guarding
                // onKeyDown here keeps the popover self-contained.
                <div
                    onClick={e => e.stopPropagation()}
                    onKeyDown={e => e.stopPropagation()}
                    onKeyUp={e => e.stopPropagation()}
                    onKeyPress={e => e.stopPropagation()}
                    style={{ marginTop: 8 }}
                >
                    <SubmitApprovalButton entityType="skills" name={skill.name} isOwner={!!skill.is_owner} />
                </div>
            )}
            <div className="skill-card-actions" aria-hidden={!canDelete}>
                {canDelete && (
                    <button
                        type="button"
                        className="skill-card-delete"
                        onClick={handleDeleteClick}
                        title="Delete skill"
                        aria-label={`Delete ${skill.name}`}
                    >
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                            <polyline points="3 6 5 6 21 6" />
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                        </svg>
                    </button>
                )}
                <span className="skill-card-chevron" aria-hidden="true">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                        <path d="M9 18l6-6-6-6" />
                    </svg>
                </span>
            </div>
            <HoverTooltip
                id={tooltip.tooltipId}
                placement={tooltip.placement}
                visible={tooltip.visible}
                title={skill.name}
                body={skill.description}
            />
        </div>
    );
}

// ── Section header ────────────────────────────────────────────────────────────

function SectionHeader({ title, count, subtitle }) {
    return (
        <header className="skills-section-header">
            <div className="skills-section-title-row">
                <h2 className="skills-section-title">{title}</h2>
                <span className="skills-section-count">{count}</span>
            </div>
            {subtitle && <p className="skills-section-subtitle">{subtitle}</p>}
        </header>
    );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function SkillsDashboard() {
    const pageRef = useRef(null);
    // 'all' is the no-filter sentinel; otherwise lowercase category key.
    const [activeCategory, setActiveCategory] = useState('all');
    const [activeSource, setActiveSource] = useState('all');
    // Governance/lifecycle scope: 'all' | 'mine' | 'pending' | 'approved'.
    // Client-side filter on the enriched fields returned by /skills-catalog.
    const [activeStage, setActiveStage] = useState('all');
    const [search, setSearch] = useState('');
    const [skills, setSkills] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [showFactory, setShowFactory] = useState(false);
    const [showUpload, setShowUpload] = useState(false);
    const [selectedSkill, setSelectedSkill] = useState(null);

    useEffect(() => {
        (pageRef.current?.closest('.dashboard-content-area') || pageRef.current?.closest('.main-content'))
            ?.scrollTo({ top: 0, behavior: 'auto' });
    }, []);

    const fetchCatalog = useCallback(async () => {
        setLoading(true);
        try {
            const res = await fetch(`${API_BASE}/skills-catalog`, { headers: buildAuthHeaders() });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            setSkills(data.skills || []);
        } catch (e) {
            setError(`Could not load catalog: ${e.message}`);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { fetchCatalog(); }, [fetchCatalog]);

    const handleSkillCreated = useCallback((skill) => {
        setShowFactory(false);
        setSkills((prev) => {
            const exists = prev.some((s) => s.name === skill.name);
            return exists
                ? prev.map((s) => (s.name === skill.name ? { ...s, ...skill } : s))
                : [skill, ...prev];
        });
    }, []);

    const handleSkillUploaded = useCallback((skill) => {
        setShowUpload(false);
        setSkills((prev) => {
            const exists = prev.some((s) => s.name === skill.name);
            return exists
                ? prev.map((s) => (s.name === skill.name ? { ...s, ...skill } : s))
                : [skill, ...prev];
        });
    }, []);

    // Modal calls this AFTER it has issued DELETE itself; we only need to
    // prune local state.
    const handleDelete = useCallback((name) => {
        setSkills((prev) => prev.filter((s) => s.name !== name));
    }, []);

    // Modal calls this AFTER a successful edit save. Merge the fresh row into
    // local state so the card/meta reflects the updated content immediately.
    const handleUpdated = useCallback((skill) => {
        if (!skill?.name) return;
        setSkills((prev) => prev.map((s) => (s.name === skill.name ? { ...s, ...skill } : s)));
    }, []);

    // Card delete path — hits the backend directly so the user doesn't need
    // to open the modal first to remove an AI-generated skill.
    const handleDeleteFromCard = useCallback(async (skill) => {
        try {
            const res = await fetch(`${API_BASE}/skills-catalog/${encodeURIComponent(skill.name)}`, {
                method: 'DELETE',
                headers: buildAuthHeaders(),
            });
            if (!res.ok && res.status !== 404) {
                throw new Error(`HTTP ${res.status}`);
            }
            setSkills((prev) => prev.filter((s) => s.name !== skill.name));
        } catch (err) {
            setError(`Could not delete "${skill.name}": ${err.message}`);
        }
    }, []);

    const builtinSkills = skills.filter((s) => getSkillSource(s) === 'builtin');
    const aiSkills      = skills.filter((s) => getSkillSource(s) === 'ai');
    const uploadSkills  = skills.filter((s) => getSkillSource(s) === 'upload');

    // Only show category chips that actually contain at least one skill in the
    // currently visible source — keeps the chip row tight on small libraries.
    const sourceFilteredSkills = activeSource === 'all'
        ? skills
        : skills.filter((s) => getSkillSource(s) === activeSource);
    const categories = deriveCategories(sourceFilteredSkills);

    const filtered = skills.filter((s) => {
        const src = getSkillSource(s);
        const matchSource = activeSource === 'all' || src === activeSource;
        const matchCat = activeCategory === 'all'
            || (s.category || 'general').toLowerCase() === activeCategory;
        const matchSearch = !search
            || s.name.toLowerCase().includes(search.toLowerCase())
            || (s.description || '').toLowerCase().includes(search.toLowerCase());
        let matchStage = true;
        if (activeStage === 'mine') {
            matchStage = !!s.is_owner;
        } else if (activeStage === 'pending') {
            matchStage = s.status === 'PENDING_APPROVAL' || s.status === 'PENDING_L2';
        } else if (activeStage === 'approved') {
            matchStage = s.status === 'APPROVED' || s.status === 'PRODUCTION' || s.status === 'ACTIVE'
                || s.generated === false; // built-ins are effectively approved
        }
        return matchSource && matchCat && matchSearch && matchStage;
    });

    const showGrouped = activeSource === 'all' && !search && activeCategory === 'all' && activeStage === 'all';

    // If a category is selected that's no longer in the catalog (e.g. user
    // switched source and that category has no skills under the new source),
    // silently fall back to "All". `categories.all` covers the full set so we
    // only snap back when the category truly no longer exists.
    useEffect(() => {
        if (activeCategory === 'all') return;
        if (!categories.all.some((c) => c.key === activeCategory)) {
            setActiveCategory('all');
        }
    }, [activeCategory, categories]);

    return (
        <div ref={pageRef} className="dashboard skills-dashboard">
            {/* Page header */}
            <header className="skills-header">
                <div className="skills-header-text">
                    <h1 className="skills-title">Skills</h1>
                    <p className="skills-subtitle">
                        Skills in the catalog are available to agents in workflows.
                        Newly created or uploaded skills require approval before they can be attached to agents.
                    </p>
                </div>
                <div className="skills-header-right">
                    {/* Stat cards double as the source filter — click to scope
                        the catalog to that source, click again (or "All") to clear. */}
                    <div className="skills-stats" role="tablist" aria-label="Filter by source">
                        <button
                            type="button"
                            role="tab"
                            aria-selected={activeSource === 'all'}
                            className={`skills-stat skills-stat-all ${activeSource === 'all' ? 'is-active' : ''}`}
                            onClick={() => setActiveSource('all')}
                        >
                            <span className="skills-stat-value">{loading ? '—' : skills.length}</span>
                            <span className="skills-stat-label">All</span>
                        </button>
                        <button
                            type="button"
                            role="tab"
                            aria-selected={activeSource === 'builtin'}
                            className={`skills-stat skills-stat-builtin ${activeSource === 'builtin' ? 'is-active' : ''}`}
                            onClick={() => setActiveSource(activeSource === 'builtin' ? 'all' : 'builtin')}
                        >
                            <span className="skills-stat-value">{loading ? '—' : builtinSkills.length}</span>
                            <span className="skills-stat-label">Built-in</span>
                        </button>
                        <button
                            type="button"
                            role="tab"
                            aria-selected={activeSource === 'ai'}
                            className={`skills-stat skills-stat-ai ${activeSource === 'ai' ? 'is-active' : ''}`}
                            onClick={() => setActiveSource(activeSource === 'ai' ? 'all' : 'ai')}
                        >
                            <span className="skills-stat-value">{loading ? '—' : aiSkills.length}</span>
                            <span className="skills-stat-label">AI Generated</span>
                        </button>
                        <button
                            type="button"
                            role="tab"
                            aria-selected={activeSource === 'upload'}
                            className={`skills-stat skills-stat-upload ${activeSource === 'upload' ? 'is-active' : ''}`}
                            onClick={() => setActiveSource(activeSource === 'upload' ? 'all' : 'upload')}
                        >
                            <span className="skills-stat-value">{loading ? '—' : uploadSkills.length}</span>
                            <span className="skills-stat-label">Uploaded</span>
                        </button>
                    </div>
                    <button
                        type="button"
                        className="skills-upload-btn"
                        onClick={() => setShowUpload(true)}
                    >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                            <polyline points="17 8 12 3 7 8" />
                            <line x1="12" y1="3" x2="12" y2="15" />
                        </svg>
                        Upload Skill
                    </button>
                    <button
                        type="button"
                        className="skills-create-btn"
                        onClick={() => setShowFactory(true)}
                    >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M12 2L15 9L22 9L16.5 13.5L18.5 21L12 16.5L5.5 21L7.5 13.5L2 9L9 9L12 2Z" />
                        </svg>
                        Create with AI
                    </button>
                </div>
            </header>

            {error && <div className="skills-error-banner">{error}</div>}

            {/* Filters — category chips are auto-derived from the catalog,
                source filter lives on the stat cards above. We only show the
                top N busiest categories so a long-tail of one-skill buckets
                doesn't drown the chip row. */}
            <div className="skills-filters">
                {/* Lifecycle scope: quick way to jump to "my drafts" or
                    "awaiting approval". Reuses the same chip styling as the
                    category chips so no new CSS is needed. */}
                <div className="skills-filter-group skills-filter-group--secondary" role="tablist" aria-label="Stage">
                    {[
                        ['all',      'All'],
                        ['mine',     'Mine'],
                        ['pending',  'Awaiting approval'],
                        ['approved', 'Approved'],
                    ].map(([key, label]) => (
                        <button
                            key={key}
                            type="button"
                            role="tab"
                            aria-selected={activeStage === key}
                            className={`skills-chip ${activeStage === key ? 'is-active' : ''}`}
                            onClick={() => setActiveStage(key)}
                        >
                            {label}
                        </button>
                    ))}
                </div>
                {categories.top.length > 0 && (
                    <div className="skills-filter-group skills-filter-group--secondary" role="tablist" aria-label="Category">
                        <button
                            type="button"
                            role="tab"
                            aria-selected={activeCategory === 'all'}
                            className={`skills-chip ${activeCategory === 'all' ? 'is-active' : ''}`}
                            onClick={() => setActiveCategory('all')}
                        >
                            All
                        </button>
                        {categories.top.map((cat) => (
                            <button
                                key={cat.key}
                                type="button"
                                role="tab"
                                aria-selected={activeCategory === cat.key}
                                className={`skills-chip ${activeCategory === cat.key ? 'is-active' : ''}`}
                                onClick={() => setActiveCategory(cat.key)}
                            >
                                {cat.label}
                                <span className="skills-chip-count">{cat.count}</span>
                            </button>
                        ))}
                        {/* Surface the long-tail as a non-clickable hint so users
                            know to use search instead of expecting more chips. */}
                        {categories.hiddenCount > 0 && (
                            <span className="skills-chip-more" title="Use search to find these">
                                +{categories.hiddenCount} more
                            </span>
                        )}
                    </div>
                )}

                <div className="skills-search-wrap">
                    <svg className="skills-search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                        <circle cx="11" cy="11" r="8" />
                        <line x1="21" y1="21" x2="16.65" y2="16.65" />
                    </svg>
                    <input
                        type="text"
                        className="skills-search"
                        placeholder="Search skills…"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                    />
                    {search && (
                        <button type="button" className="skills-search-clear" onClick={() => setSearch('')} aria-label="Clear search">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                                <line x1="18" y1="6" x2="6" y2="18" />
                                <line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                        </button>
                    )}
                </div>
            </div>

            {/* Content */}
            {loading ? (
                <div className="skills-empty">
                    <span className="skills-empty-loading">Loading skills…</span>
                </div>
            ) : filtered.length === 0 ? (
                <div className="skills-empty">
                    <div className="skills-empty-icon" aria-hidden="true">
                        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
                        </svg>
                    </div>
                    <h3 className="skills-empty-title">
                        {skills.length === 0 ? 'No skills in catalog yet' : 'No skills match your filters'}
                    </h3>
                    <p className="skills-empty-text">
                        {skills.length === 0
                            ? 'Use "Create with AI" to generate and add a skill to the catalog.'
                            : 'Try clearing a filter or searching for a different term.'}
                    </p>
                </div>
            ) : showGrouped ? (
                <>
                    {builtinSkills.length > 0 && (
                        <section className="skills-section">
                            <SectionHeader
                                title="Built-in Skills"
                                count={builtinSkills.length}
                                subtitle="Core skills that ship with the platform — always available to agents."
                            />
                            <div className="skills-grid">
                                {builtinSkills.map((skill) => (
                                    <SkillCard key={skill.name} skill={skill} onClick={setSelectedSkill} onDelete={handleDeleteFromCard} />
                                ))}
                            </div>
                        </section>
                    )}
                    {aiSkills.length > 0 && (
                        <section className="skills-section">
                            <SectionHeader
                                title="AI Generated"
                                count={aiSkills.length}
                                subtitle="Custom skills created with the Skill Factory."
                            />
                            <div className="skills-grid">
                                {aiSkills.map((skill) => (
                                    <SkillCard key={skill.name} skill={skill} onClick={setSelectedSkill} onDelete={handleDeleteFromCard} />
                                ))}
                            </div>
                        </section>
                    )}
                    {uploadSkills.length > 0 && (
                        <section className="skills-section">
                            <SectionHeader
                                title="Uploaded"
                                count={uploadSkills.length}
                                subtitle="Skills imported from a packaged .zip / .skill bundle."
                            />
                            <div className="skills-grid">
                                {uploadSkills.map((skill) => (
                                    <SkillCard key={skill.name} skill={skill} onClick={setSelectedSkill} onDelete={handleDeleteFromCard} />
                                ))}
                            </div>
                        </section>
                    )}
                </>
            ) : (
                <div className="skills-grid">
                    {filtered.map((skill) => (
                        <SkillCard key={skill.name} skill={skill} onClick={setSelectedSkill} onDelete={handleDeleteFromCard} />
                    ))}
                </div>
            )}

            {selectedSkill && (
                <SkillDetailModal
                    skill={selectedSkill}
                    onClose={() => setSelectedSkill(null)}
                    onDelete={handleDelete}
                    onUpdated={handleUpdated}
                />
            )}

            {showFactory && (
                <SkillFactoryChat
                    onClose={() => setShowFactory(false)}
                    onCreated={handleSkillCreated}
                />
            )}

            {showUpload && (
                <SkillUploadModal
                    onClose={() => setShowUpload(false)}
                    onUploaded={handleSkillUploaded}
                />
            )}
        </div>
    );
}
