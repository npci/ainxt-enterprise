// SPDX-License-Identifier: MIT
/**
 * Sample Document (look-and-feel reference) section.
 *
 * Reusable UI shared by:
 *   - Agent editor        → `AgentEditor.jsx`     (per-agent sample stored in `agents.sample_doc`)
 *   - Workflow node panel → `ConfigPanel.jsx`     (per-node sample stored inline on node.data.sample_doc)
 *
 * Contract:
 *   - ``value``       — current sample metadata object (or empty ``{}`` when none). Shape:
 *                       ``{ path, kind, name, size_bytes, notes, uploaded_at, download_url? }``.
 *   - ``onChange``    — called with the new metadata after upload / clear / notes save.
 *                       For the workflow use-case this is where the parent syncs
 *                       ``node.data.sample_doc``; for the agent editor it just updates local state.
 *   - ``endpoint``    — { upload, get, download, del } endpoint URLs (all POST/GET/DELETE-style).
 *                       ``upload`` accepts multipart {file, notes}. ``del`` is DELETE-only.
 *                       Any of them may be ``null`` — the section shows a hint asking the user to
 *                       save the parent (agent name / workflow) before attaching.
 *   - ``notReadyHint``— short string shown in place of the picker when ``endpoint`` is null.
 *                       e.g. "Save the agent first" / "Save the workflow first".
 *
 * Design notes:
 *   - Filename box uses a two-line layout with ``overflow-hidden`` + ``text-overflow: ellipsis``
 *     so a long ``some_really_long_business_requirements_document_v3_final.docx`` never overflows
 *     the parent card. The kind badge / size / Download link sit on a second row so all three
 *     stay visible regardless of filename length.
 *   - Buttons keep the same border-and-color language as ``KnowledgeUploadInline`` so this
 *     section blends in whether it lives in the standalone-agent tall right column or in the
 *     narrow workflow ConfigPanel.
 */
import { useCallback, useState } from 'react';
import { API_BASE, buildAuthHeaders } from '../../config/api';

const _NOTES_MAX = 2000;
const _ACCEPT = '.docx,.pptx,.xlsx,.pdf';
const _ALLOWED_HINT = 'Allowed: .docx, .pptx, .xlsx, .pdf · up to 25 MB';


function _humanSize(n) {
    if (typeof n !== 'number' || !isFinite(n)) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
}


export default function SampleDocSection({
    value,
    onChange,
    endpoint,
    notReadyHint = 'Save first, then attach a sample.',
}) {
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [notesDraft, setNotesDraft] = useState((value && value.notes) || '');
    // Keep notesDraft in sync when the parent hands us a fresh value
    // (e.g. after selecting a different workflow node).
    const [lastValueKey, setLastValueKey] = useState(value && value.path);
    if ((value && value.path) !== lastValueKey) {
        setLastValueKey(value && value.path);
        setNotesDraft((value && value.notes) || '');
    }

    const sample = value || {};
    const attached = !!sample.path;

    const uploadFile = useCallback(async (file, notesOverride) => {
        if (!endpoint?.upload) {
            setError(notReadyHint);
            return;
        }
        setError('');
        setBusy(true);
        try {
            const formData = new FormData();
            formData.append('file', file);
            const notesValue = (notesOverride ?? notesDraft ?? '').trim();
            if (notesValue) formData.append('notes', notesValue);
            const res = await fetch(endpoint.upload, {
                method: 'POST',
                credentials: 'include',
                headers: buildAuthHeaders({ omitContentType: true }),
                body: formData,
            });
            let data = null;
            try { data = await res.json(); } catch { /* non-JSON error body */ }
            if (!res.ok) {
                const reason = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
                throw new Error(reason);
            }
            onChange && onChange(data || {});
            setNotesDraft((data && data.notes) || '');
        } catch (err) {
            setError(err?.message || 'Upload failed.');
        } finally {
            setBusy(false);
        }
    }, [endpoint, onChange, notReadyHint, notesDraft]);

    const clear = useCallback(async () => {
        if (!endpoint?.del) return;
        setError('');
        setBusy(true);
        try {
            const res = await fetch(endpoint.del, {
                method: 'DELETE',
                credentials: 'include',
                headers: buildAuthHeaders(),
            });
            if (!res.ok && res.status !== 204) {
                let reason = `HTTP ${res.status}`;
                try {
                    const j = await res.json();
                    reason = j?.detail || j?.message || reason;
                } catch { /* ignore */ }
                throw new Error(reason);
            }
            onChange && onChange({});
            setNotesDraft('');
        } catch (err) {
            setError(err?.message || 'Could not remove sample.');
        } finally {
            setBusy(false);
        }
    }, [endpoint, onChange]);

    // Notes-only re-upload. The upload endpoint is the only writer of the
    // ``notes`` field, so we fetch the current bytes and re-post with the
    // updated notes.
    const saveNotes = useCallback(async () => {
        if (!attached || !endpoint?.download || !endpoint?.upload) return;
        setError('');
        setBusy(true);
        try {
            const dl = await fetch(endpoint.download, {
                credentials: 'include',
                headers: buildAuthHeaders(),
            });
            if (!dl.ok) throw new Error(`Could not read current sample (HTTP ${dl.status})`);
            const blob = await dl.blob();
            const filename = sample.name || `sample.${sample.kind || 'bin'}`;
            const file = new File([blob], filename, { type: blob.type || 'application/octet-stream' });
            await uploadFile(file, notesDraft);
        } catch (err) {
            setError(err?.message || 'Could not save notes.');
        } finally {
            setBusy(false);
        }
    }, [attached, endpoint, sample, notesDraft, uploadFile]);

    if (!endpoint) {
        return (
            <div style={{ fontSize: 11, color: '#6b7280' }}>
                {notReadyHint}
            </div>
        );
    }

    return (
        <div>
            {attached ? (
                <div
                    style={{
                        border: '1px solid #e5e7eb',
                        borderRadius: 6,
                        padding: '10px 12px',
                        background: '#f9fafb',
                    }}
                >
                    {/* Row 1: filename (truncates on overflow so a 90-char name
                        can't blow up the card width, especially in the narrow
                        workflow ConfigPanel). */}
                    <div
                        title={sample.name || `sample.${sample.kind}`}
                        style={{
                            fontWeight: 600,
                            fontSize: 13,
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            minWidth: 0,
                        }}
                    >
                        {sample.name || `sample.${sample.kind}`}
                    </div>

                    {/* Row 2: kind badge + size + download link — sits below so
                        it stays visible regardless of filename length. */}
                    <div
                        style={{
                            marginTop: 6,
                            display: 'flex',
                            alignItems: 'center',
                            gap: 8,
                            flexWrap: 'wrap',
                            fontSize: 11,
                            color: '#6b7280',
                        }}
                    >
                        <span
                            style={{
                                fontSize: 10,
                                padding: '1px 6px',
                                borderRadius: 4,
                                background: '#e5e7eb',
                                color: '#374151',
                                textTransform: 'uppercase',
                                letterSpacing: 0.4,
                            }}
                        >
                            {sample.kind}
                        </span>
                        {typeof sample.size_bytes === 'number' && (
                            <span>{_humanSize(sample.size_bytes)}</span>
                        )}
                        {sample.download_url && (
                            <a
                                href={`${API_BASE}${sample.download_url}`}
                                style={{ marginLeft: 'auto' }}
                            >
                                Download
                            </a>
                        )}
                    </div>

                    {/* Row 3: replace / remove buttons. */}
                    <div style={{ marginTop: 10, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        <label
                            style={{
                                padding: '6px 12px',
                                fontSize: 12,
                                fontWeight: 500,
                                color: busy ? '#9ca3af' : '#4f46e5',
                                background: 'white',
                                border: `1px solid ${busy ? '#e5e7eb' : '#c7d2fe'}`,
                                borderRadius: 6,
                                cursor: busy ? 'wait' : 'pointer',
                            }}
                        >
                            Replace file
                            <input
                                type="file"
                                accept={_ACCEPT}
                                hidden
                                disabled={busy}
                                onChange={(e) => {
                                    const f = e.target.files?.[0];
                                    e.target.value = '';
                                    if (f) uploadFile(f);
                                }}
                            />
                        </label>
                        <button
                            type="button"
                            style={{
                                padding: '6px 12px',
                                fontSize: 12,
                                fontWeight: 500,
                                color: busy ? '#9ca3af' : '#b91c1c',
                                background: 'white',
                                border: `1px solid ${busy ? '#e5e7eb' : '#fecaca'}`,
                                borderRadius: 6,
                                cursor: busy ? 'wait' : 'pointer',
                            }}
                            disabled={busy}
                            onClick={clear}
                        >
                            Remove
                        </button>
                    </div>
                </div>
            ) : (
                <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                    <label
                        style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: 6,
                            padding: '8px 16px',
                            fontSize: 12,
                            fontWeight: 600,
                            color: busy ? '#9ca3af' : '#ffffff',
                            background: busy ? '#e5e7eb' : '#4f46e5',
                            border: '1px solid transparent',
                            borderRadius: 6,
                            cursor: busy ? 'wait' : 'pointer',
                            lineHeight: 1,
                        }}
                    >
                        {busy ? 'Uploading…' : 'Upload sample'}
                        <input
                            type="file"
                            accept={_ACCEPT}
                            hidden
                            disabled={busy}
                            onChange={(e) => {
                                const f = e.target.files?.[0];
                                e.target.value = '';
                                if (f) uploadFile(f);
                            }}
                        />
                    </label>
                    <span style={{ fontSize: 11, color: '#6b7280' }}>
                        {_ALLOWED_HINT}
                    </span>
                </div>
            )}

            <div style={{ marginTop: 10 }}>
                <label
                    style={{
                        display: 'block',
                        fontSize: 11,
                        fontWeight: 500,
                        color: '#374151',
                        marginBottom: 4,
                    }}
                    htmlFor="sample-doc-notes"
                >
                    Notes for the model (optional)
                </label>
                <textarea
                    id="sample-doc-notes"
                    rows={3}
                    maxLength={_NOTES_MAX}
                    placeholder='e.g. "Keep the cover page and the branded footer; rewrite everything else."'
                    value={notesDraft}
                    onChange={(e) => setNotesDraft(e.target.value)}
                    onBlur={() => {
                        // Only persist notes when a sample exists AND the notes
                        // actually changed — avoids re-uploading the same file
                        // on every focus/blur cycle.
                        if (!attached) return;
                        if ((sample.notes || '') === notesDraft) return;
                        saveNotes();
                    }}
                    disabled={busy}
                    style={{
                        width: '100%',
                        padding: '8px 10px',
                        fontSize: 12,
                        border: '1px solid #e5e7eb',
                        borderRadius: 6,
                        fontFamily: 'inherit',
                        resize: 'vertical',
                        boxSizing: 'border-box',
                    }}
                />
            </div>

            {error && (
                <div style={{ fontSize: 11, color: '#b91c1c', marginTop: 6 }}>
                    {error}
                </div>
            )}
        </div>
    );
}
