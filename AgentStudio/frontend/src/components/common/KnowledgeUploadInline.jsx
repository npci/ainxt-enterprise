// SPDX-License-Identifier: MIT
/**
 * KnowledgeUploadInline — embedded multi-file uploader for the Build Studio
 * "Add KB" mode.
 *
 * Uploads through the SAME endpoint the sidebar Knowledge Base page uses —
 * ``POST /ainxt/v1/api/kb/upload`` (platform side, PENDING_APPROVAL queue).
 * A doc uploaded here is indistinguishable at the graph level from one
 * uploaded via the sidebar and appears in the platform-wide Knowledge Base
 * once approved.
 *
 * Field set — matched 1:1 with the sidebar upload panel:
 *   • Scope: Domain + Product + Spec Version + optional Version Date /
 *     Source Type / Deprecate Prior. Supplied via the parent's
 *     KbUploadScopePicker; passed to us on the ``scope`` prop.
 *   • Visibility: Public (org-wide) or Private (department-scoped).
 *   • Department Access: multi-select when the caller is an approver
 *     (``isApprover=true``); locked banner otherwise.
 *
 * Behaviour to match the sidebar (per user request):
 *   • The drop zone is ALWAYS enabled. Files can be dropped/browsed before
 *     the scope + visibility fields are filled. The user-visible warnings
 *     only appear when an upload is actually attempted with something
 *     missing — same UX as ``scopeWarn`` in ai-ui/…/KnowledgeBase.jsx.
 *   • Namespace is derived from the scope's domain (matches the sidebar's
 *     ``const ns = specDomain.trim();`` convention). No separate namespace
 *     input.
 *
 * Props:
 *   scope           — Required scope from KbUploadScopePicker. Shape:
 *                     { product_id, domain, spec_version, source_type?,
 *                       version_date?, deprecate_prior? }.
 *   userDept        — current user's department. Used to lock the
 *                     department multi-select for non-approvers.
 *   isApprover      — when true, department multi-select is shown; when
 *                     false, dept is locked to the uploader's own.
 *   onUploaded      — callback fired once per successfully uploaded file.
 *                     Shape: { doc_id, chunk_count, namespace, filename,
 *                     status, product_id?, domain?, spec_version?,
 *                     source_type? }.
 */
import { useEffect, useRef, useState } from 'react';
import { platformFetch, kbFetch } from '../../config/api';
import PortalMenu from './PortalMenu';

// Split a file list by allowed extension.
function _partitionByExt(files, allowed) {
    const valid = [];
    const invalid = [];
    files.forEach(f => {
        const ext = (f.name.split('.').pop() || '').toLowerCase();
        if (allowed.includes(ext)) valid.push(f);
        else invalid.push(f.name);
    });
    return { valid, invalid };
}

// Kept in lockstep with the sidebar KB uploader.
const SUPPORTED_TYPES = ['PDF', 'DOCX', 'MD', 'PPTX', 'HTML', 'TXT', 'XLSX', 'XLS', 'CSV'];
const ALLOWED_EXTS = ['pdf', 'docx', 'md', 'ppt', 'pptx', 'html', 'txt', 'xlsx', 'xls', 'csv'];
const MAX_SIZE_BYTES = 25 * 1024 * 1024;
const MAX_FILES_PER_BATCH = 5;
const UPLOAD_CONCURRENCY = 3;

const STAGES = [
    { key: 'parse', label: 'Parsing document',         detail: 'Extracting text content' },
    { key: 'chunk', label: 'Creating chunks',           detail: 'Splitting into searchable pieces' },
    { key: 'embed', label: 'Embedding with AI',         detail: 'Generating vector embeddings' },
    { key: 'save',  label: 'Saving to knowledge base',  detail: 'Persisting to pgvector + DB' },
];
const STAGE_ORDER = STAGES.map(s => s.key);
const STAGE_TIMERS = { parse: 700, chunk: 900 };

let _localCounter = 0;
function _newId() {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
        return crypto.randomUUID();
    }
    _localCounter += 1;
    return `local-${Date.now()}-${_localCounter}`;
}

function fmtSize(bytes) {
    if (!bytes) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function useFileDrop({ disabled, onFiles }) {
    const dropRef = useRef(null);
    const [isDragging, setIsDragging] = useState(false);
    const onFilesRef = useRef(onFiles);
    useEffect(() => { onFilesRef.current = onFiles; }, [onFiles]);

    useEffect(() => {
        const el = dropRef.current;
        if (!el || disabled) return undefined;

        const onDragEnter = (e) => { e.preventDefault(); e.stopPropagation(); setIsDragging(true); };
        const onDragOver  = (e) => { e.preventDefault(); e.stopPropagation(); };
        const onDragLeave = (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (!el.contains(e.relatedTarget)) setIsDragging(false);
        };
        const onDrop = (e) => {
            e.preventDefault();
            e.stopPropagation();
            setIsDragging(false);
            const files = Array.from(e.dataTransfer?.files || []);
            if (files.length > 0) onFilesRef.current(files);
        };

        el.addEventListener('dragenter', onDragEnter);
        el.addEventListener('dragover',  onDragOver);
        el.addEventListener('dragleave', onDragLeave);
        el.addEventListener('drop',      onDrop);
        return () => {
            el.removeEventListener('dragenter', onDragEnter);
            el.removeEventListener('dragover',  onDragOver);
            el.removeEventListener('dragleave', onDragLeave);
            el.removeEventListener('drop',      onDrop);
        };
    }, [disabled]);

    return { isDragging, dropRef };
}

// ── Per-file progress / done / error / compliance-block card ────────────
function UploadProgress({ record, onDismiss }) {
    const { file, stage, result, error, complianceBlock } = record;
    const currentIdx = STAGE_ORDER.indexOf(stage);
    const isDone = stage === 'done';
    const isError = stage === 'error';
    const isBlocked = stage === 'blocked';

    if (isBlocked && complianceBlock) {
        return (
            <div style={{ border: '1px solid #fecaca', background: '#fef2f2', borderRadius: 8, padding: 12 }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
                    <span style={{ color: '#ef4444', fontSize: 16 }}>⛔</span>
                    <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ fontSize: 12, fontWeight: 600, color: '#b91c1c' }}>
                            File blocked by compliance policy
                        </div>
                        <div style={{ fontSize: 12, color: '#dc2626', marginTop: 2 }}>{complianceBlock.filename}</div>
                        {complianceBlock.compliance_reasons && complianceBlock.compliance_reasons.length > 0 && (
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 6 }}>
                                {complianceBlock.compliance_reasons.map(r => (
                                    <span key={r} style={{
                                        background: '#fee2e2', color: '#b91c1c',
                                        border: '1px solid #fecaca', fontSize: 10,
                                        padding: '2px 6px', borderRadius: 3, fontWeight: 500,
                                    }}>{r}</span>
                                ))}
                            </div>
                        )}
                        <div style={{ fontSize: 10, color: '#f87171', marginTop: 6 }}>
                            This file contains sensitive data and cannot be added to the knowledge base.
                        </div>
                    </div>
                    <button onClick={onDismiss} style={{ background: 'none', border: 'none', color: '#f87171', cursor: 'pointer' }}>×</button>
                </div>
            </div>
        );
    }

    return (
        <div style={{ border: '1px solid #e5e7eb', borderRadius: 8, overflow: 'hidden' }}>
            <div style={{
                padding: '10px 14px', display: 'flex', alignItems: 'center', gap: 12,
                background: isDone ? '#ecfdf5' : isError ? '#fef2f2' : '#f9fafb',
            }}>
                <span style={{ width: 16, height: 16, color: isDone ? '#10b981' : isError ? '#ef4444' : '#9ca3af' }}>
                    {isDone ? '✓' : isError ? '×' : '⟳'}
                </span>
                <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontSize: 12, fontWeight: 500, color: '#1f2937', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {file.name}
                    </div>
                    <div style={{ fontSize: 10, color: isError ? '#dc2626' : '#9ca3af' }}>
                        {fmtSize(file.size)}
                        {isDone && result && (
                            <span style={{ marginLeft: 8, color: '#10b981', fontWeight: 500 }}>
                                ✓ Submitted for approval — will appear once approved
                            </span>
                        )}
                        {isError && error && (
                            <span style={{ marginLeft: 8, color: '#dc2626', fontWeight: 500 }}>{error}</span>
                        )}
                    </div>
                </div>
                {(isDone || isError) && (
                    <button onClick={onDismiss} style={{ background: 'none', border: 'none', color: '#9ca3af', cursor: 'pointer', fontSize: 14 }}>×</button>
                )}
            </div>
            {!isError && (
                <div style={{ padding: '10px 14px', display: 'flex', flexDirection: 'column', gap: 10 }}>
                    {STAGES.map(s => {
                        const stageIdx = STAGE_ORDER.indexOf(s.key);
                        const isActive = !isDone && s.key === stage;
                        const isStepDone = isDone || stageIdx < currentIdx;
                        return (
                            <div key={s.key} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                                <div style={{ width: 16, textAlign: 'center', color: isStepDone ? '#10b981' : isActive ? '#3b82f6' : '#e5e7eb' }}>
                                    {isStepDone ? '●' : isActive ? '◐' : '○'}
                                </div>
                                <div style={{ flex: 1, minWidth: 0 }}>
                                    <span style={{
                                        fontSize: 12,
                                        color: isStepDone ? '#9ca3af' : isActive ? '#1f2937' : '#d1d5db',
                                        textDecoration: isStepDone ? 'line-through' : 'none',
                                        fontWeight: isActive ? 500 : 400,
                                    }}>{s.label}</span>
                                    {isActive && (
                                        <div style={{ fontSize: 10, color: '#60a5fa', marginTop: 2 }}>{s.detail}</div>
                                    )}
                                </div>
                                {isActive && s.key === 'embed' && (
                                    <span style={{ fontSize: 10, color: '#60a5fa' }}>may take a moment…</span>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

// ── Multi-select department dropdown ───────────────────────────────────
// Identical UX to the sidebar's MultiSelectDept so approvers see the same
// searchable pill-list they're used to.
function MultiSelectDept({ options, selected, onChange }) {
    const [open, setOpen] = useState(false);
    const [search, setSearch] = useState('');
    const ref = useRef(null);

    // Outside-click handling is delegated to <PortalMenu> (it checks both the
    // anchor and the portaled menu, which lives outside this subtree).

    const filtered = options.filter(o => o && o.toLowerCase().includes(search.toLowerCase()));
    const toggle = (d) => onChange(selected.includes(d) ? selected.filter(x => x !== d) : [...selected, d]);

    return (
        <div ref={ref} style={{ position: 'relative' }}>
            <div
                onClick={() => setOpen(o => !o)}
                style={{
                    minHeight: 38, width: '100%',
                    background: 'white', border: '1px solid #d1d5db',
                    borderRadius: 4, padding: '6px 8px',
                    display: 'flex', flexWrap: 'wrap', gap: 4, cursor: 'pointer',
                }}
            >
                {selected.length === 0 && (
                    <span style={{ color: '#9ca3af', fontSize: 13, alignSelf: 'center' }}>Select departments…</span>
                )}
                {selected.map(d => (
                    <span key={d} style={{
                        display: 'flex', alignItems: 'center', gap: 4,
                        background: 'linear-gradient(to bottom right, #4f46e5, #7c3aed)',
                        color: 'white', fontSize: 11, padding: '2px 8px', borderRadius: 999,
                    }}>
                        {d}
                        <button type="button" onClick={(e) => { e.stopPropagation(); onChange(selected.filter(x => x !== d)); }}
                            style={{ background: 'none', border: 'none', color: 'white', cursor: 'pointer', fontSize: 12, lineHeight: 1 }}>×</button>
                    </span>
                ))}
                <span style={{ marginLeft: 'auto', color: '#9ca3af', alignSelf: 'center' }}>{open ? '▴' : '▾'}</span>
            </div>
            <PortalMenu
                anchorRef={ref}
                open={open}
                onRequestClose={() => setOpen(false)}
                style={{ padding: 0 }}
            >
                    <div style={{ padding: 8, borderBottom: '1px solid #f3f4f6' }}>
                        <input
                            autoFocus value={search} onChange={(e) => setSearch(e.target.value)}
                            placeholder="Search departments…"
                            style={{ width: '100%', fontSize: 13, padding: '4px 8px', border: '1px solid #e5e7eb', borderRadius: 4, outline: 'none' }}
                        />
                    </div>
                    <div style={{ maxHeight: 200, overflowY: 'auto', padding: 6 }}>
                        {filtered.length === 0 && (
                            <div className="kb-scope-menu__empty">No departments found</div>
                        )}
                        {filtered.map(d => (
                            <label
                                key={d}
                                className={
                                    'kb-scope-menu__item'
                                    + (selected.includes(d) ? ' kb-scope-menu__item--active' : '')
                                }
                                style={{ display: 'flex', alignItems: 'center', gap: 8 }}
                            >
                                <input type="checkbox" checked={selected.includes(d)} onChange={() => toggle(d)} />
                                {d}
                            </label>
                        ))}
                    </div>
            </PortalMenu>
        </div>
    );
}

/**
 * @param {{
 *   scope: object,
 *   userDept?: string,
 *   isApprover?: boolean,
 *   onUploaded?: (result: object) => void,
 * }} props
 */
export default function KnowledgeUploadInline({
    scope = {},
    userDept = '',
    isApprover = false,
    onUploaded = () => {},
}) {
    // Visibility mirrors the sidebar: PUBLIC (org-wide) or PRIVATE (dept).
    const [visibility, setVisibility] = useState('PUBLIC');
    // Selected departments — an empty array under PUBLIC means org-wide,
    // and under PRIVATE means "no departments yet" (the user still has to
    // pick before uploading). Non-approvers see a locked banner and are
    // pinned to their own department.
    const [selectedDepts, setSelectedDepts] = useState(
        userDept && !isApprover ? [userDept] : []
    );
    const [availableDepts, setAvailableDepts] = useState([]);

    // Per-file records shown in the progress list.
    const [uploads, setUploads] = useState([]);
    const [error, setError] = useState(null);

    // Worker-pool plumbing.
    const queueRef = useRef([]);
    const activeRef = useRef(0);
    const timersRef = useRef(new Map());
    const fileInputRef = useRef(null);

    function _clearTimersFor(id) {
        const t = timersRef.current.get(id);
        if (!t) return;
        clearTimeout(t.chunk);
        clearTimeout(t.stage);
        timersRef.current.delete(id);
    }

    useEffect(() => () => {
        timersRef.current.forEach(t => {
            clearTimeout(t.chunk);
            clearTimeout(t.stage);
        });
        timersRef.current.clear();
    }, []);

    // Load approvers' department list. Non-approvers don't need it — they're
    // locked to their own dept.
    useEffect(() => {
        if (!isApprover) return undefined;
        let cancelled = false;
        (async () => {
            try {
                const res = await platformFetch('/products/departments');
                if (!res.ok) return;
                const data = await res.json();
                if (cancelled) return;
                const next = (data.departments || []).filter(d => d && d.trim() !== '');
                setAvailableDepts(prev => (
                    prev.length === next.length && prev.every((v, i) => v === next[i]) ? prev : next
                ));
            } catch { /* non-fatal */ }
        })();
        return () => { cancelled = true; };
    }, [isApprover]);

    const isUploading = uploads.some(u =>
        u.stage !== 'done' && u.stage !== 'error' && u.stage !== 'blocked'
    );

    // Readiness checks — used to render the soft warning banner and to
    // reject an upload attempt. Match the sidebar's rules 1:1.
    const scopeReady = !!(
        scope
        && scope.product_id
        && scope.domain
        && (scope.spec_version || '').trim()
    );
    const deptReady = visibility !== 'PRIVATE' || selectedDepts.length > 0;

    function _patchRecord(id, patch) {
        setUploads(prev => prev.map(u => (u.id === id ? { ...u, ...patch } : u)));
    }

    function dismissRecord(id) {
        _clearTimersFor(id);
        setUploads(prev => prev.filter(u => u.id !== id));
    }

    // Process a single file end-to-end.
    async function _processOne(id, rawFile, captured) {
        const file = { name: rawFile.name, size: rawFile.size };
        const { scope: capturedScope, visibility: capturedVisibility, selectedDepts: capturedDepts } = captured;

        const chunk = setTimeout(() => _patchRecord(id, { stage: 'chunk' }), STAGE_TIMERS.parse);
        const stage = setTimeout(() => _patchRecord(id, { stage: 'embed' }), STAGE_TIMERS.parse + STAGE_TIMERS.chunk);
        timersRef.current.set(id, { chunk, stage });
        _patchRecord(id, { stage: 'parse' });

        try {
            const form = new FormData();
            // Namespace mirrors the sidebar convention: ns == domain.
            const ns = String(capturedScope.domain || '').trim();
            form.append('namespace', ns);
            form.append('files', rawFile);
            form.append('visibility', capturedVisibility);
            form.append('department_ids', JSON.stringify(capturedDepts));
            // Graph-model scope fields (same as sidebar).
            if (capturedScope.product_id)   form.append('product_id',   String(capturedScope.product_id));
            if (capturedScope.domain)       form.append('domain',       String(capturedScope.domain));
            if (capturedScope.spec_version) form.append('spec_version', String(capturedScope.spec_version));
            if (capturedScope.version_date) form.append('version_date', String(capturedScope.version_date));
            form.append('deprecate_prior', capturedScope.deprecate_prior ? 'true' : 'false');
            if (capturedScope.source_type)  form.append('source_type',  String(capturedScope.source_type));

            const res = await kbFetch('/upload', {
                method: 'POST',
                body: form,
                omitContentType: true,
            });

            if (res.status === 413) throw new Error('File is too large. Maximum allowed size is 25 MB.');
            if (res.status === 415) {
                let detail = 'Unsupported file type.';
                try { const j = await res.json(); detail = j.detail || detail; } catch {}
                throw new Error(detail);
            }
            const contentType = res.headers.get('content-type') || '';
            if (!contentType.includes('application/json')) {
                const body = await res.text().catch(() => '');
                throw new Error(`Upload failed (HTTP ${res.status})${body ? `: ${body.slice(0, 200)}` : ''}`);
            }
            const data = await res.json();
            if (!res.ok) {
                throw new Error(data.detail || data.error || `Upload failed (HTTP ${res.status})`);
            }

            if (data && data.blocked) {
                _clearTimersFor(id);
                _patchRecord(id, {
                    stage: 'blocked',
                    complianceBlock: {
                        filename: data.filename || file.name,
                        block_reason: data.block_reason || 'PCI/PII data',
                        compliance_reasons: data.compliance_reasons || [],
                    },
                });
                return;
            }
            if (!data || !data.success) {
                throw new Error((data && (data.error || data.detail)) || 'Upload failed');
            }

            _clearTimersFor(id);
            _patchRecord(id, { stage: 'save', result: { status: data.status } });

            await new Promise(r => setTimeout(r, 400));
            _patchRecord(id, { stage: 'done' });

            onUploaded({
                doc_id: data.doc_id,
                chunk_count: data.chunk_count || 0,
                namespace: data.namespace || ns,
                filename: data.filename || file.name,
                status: data.status,
                product_id:   capturedScope.product_id   || data.product_id   || null,
                domain:       capturedScope.domain       || data.domain       || null,
                spec_version: capturedScope.spec_version || data.spec_version || null,
                source_type:  capturedScope.source_type  || data.source_type  || null,
            });
        } catch (e) {
            _clearTimersFor(id);
            _patchRecord(id, {
                stage: 'error',
                error: e.message || 'Upload failed',
            });
        }
    }

    function _pumpQueue() {
        while (activeRef.current < UPLOAD_CONCURRENCY && queueRef.current.length > 0) {
            const next = queueRef.current.shift();
            activeRef.current += 1;
            _processOne(next.id, next.rawFile, next.captured)
                .finally(() => {
                    activeRef.current -= 1;
                    _pumpQueue();
                });
        }
    }

    function handleUpload(files) {
        if (!files || files.length === 0) return;
        // Soft warn-on-attempt — matches sidebar's ``scopeWarn`` UX. We do
        // NOT gate the drop zone; we only reject the attempt when the
        // required fields are missing.
        if (!scopeReady) {
            setError('Set Domain, Product, and Spec Version above before uploading.');
            return;
        }
        if (!deptReady) {
            setError('Select at least one department for Private uploads.');
            return;
        }
        setError(null);

        const captured = {
            scope: { ...scope },
            visibility,
            selectedDepts: [...selectedDepts],
        };

        const slotsLeft = MAX_FILES_PER_BATCH - uploads.length;
        const overflow = files.length - slotsLeft;
        const capped = overflow > 0 ? files.slice(0, Math.max(0, slotsLeft)) : files;

        const accepted = [];
        const tooLarge = [];
        capped.forEach(file => {
            if (file.size > MAX_SIZE_BYTES) { tooLarge.push(file.name); return; }
            accepted.push(file);
        });

        if (tooLarge.length > 0) {
            const list = tooLarge.length === 1
                ? `"${tooLarge[0]}" is too large`
                : `${tooLarge.length} files are too large`;
            setError(`${list}. Maximum allowed size is 25 MB per file.`);
            if (accepted.length === 0) return;
        }

        if (overflow > 0) {
            const skipped = `${overflow} file${overflow === 1 ? '' : 's'} skipped`;
            setError(
                slotsLeft <= 0
                    ? `You can upload at most ${MAX_FILES_PER_BATCH} documents at a time.`
                    : `You can upload at most ${MAX_FILES_PER_BATCH} documents at a time — ${skipped}.`
            );
            if (accepted.length === 0) return;
        }

        const newRecords = accepted.map(file => ({
            id: _newId(),
            file: { name: file.name, size: file.size },
            stage: 'queued',
            result: null,
            error: null,
            complianceBlock: null,
        }));

        setUploads(prev => [...prev, ...newRecords]);
        accepted.forEach((rawFile, i) => {
            queueRef.current.push({ id: newRecords[i].id, rawFile, captured });
        });
        _pumpQueue();
    }

    function ingestFiles(files) {
        if (!files || files.length === 0) return;
        const { valid, invalid } = _partitionByExt(files, ALLOWED_EXTS);
        if (invalid.length > 0) {
            const list = invalid.length === 1
                ? `"${invalid[0]}" has an unsupported file type`
                : `${invalid.length} files have unsupported types`;
            setError(`${list}. Allowed: PDF, DOCX, MD, PPTX, HTML, TXT, XLSX, XLS, CSV.`);
            if (valid.length === 0) return;
        }
        handleUpload(valid);
    }

    // Drop zone is ALWAYS enabled — the sidebar-parity UX the user asked for.
    // Fields can be filled in before or after picking files.
    const { isDragging, dropRef } = useFileDrop({
        disabled: false,
        onFiles: ingestFiles,
    });

    // Soft warning banner — shown when fields are still incomplete, but
    // does NOT disable the drop zone (mirrors sidebar's ``scopeWarn``).
    const missingHints = [];
    if (!scopeReady)  missingHints.push('Domain, Product, and Spec Version');
    if (!deptReady)   missingHints.push('at least one department for Private uploads');

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            {/* Visibility + department scope */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, opacity: isUploading ? 0.5 : 1, pointerEvents: isUploading ? 'none' : 'auto' }}>
                <div>
                    <label style={{ display: 'block', fontSize: 11, fontWeight: 500, color: '#374151', marginBottom: 6 }}>
                        Visibility
                    </label>
                    <div style={{ display: 'flex', gap: 8 }}>
                        {['PUBLIC', 'PRIVATE'].map(v => (
                            <button
                                key={v}
                                type="button"
                                onClick={() => {
                                    setVisibility(v);
                                    if (v === 'PUBLIC') setSelectedDepts([]);
                                    else if (isApprover) setSelectedDepts([]);
                                    else setSelectedDepts(userDept ? [userDept] : []);
                                }}
                                style={{
                                    padding: '6px 12px',
                                    borderRadius: 4,
                                    fontSize: 11,
                                    fontWeight: 500,
                                    cursor: 'pointer',
                                    border: `1px solid ${visibility === v ? '#4f46e5' : '#d1d5db'}`,
                                    background: visibility === v
                                        ? 'linear-gradient(to bottom right, #4f46e5, #7c3aed)'
                                        : '#f9fafb',
                                    color: visibility === v ? 'white' : '#4b5563',
                                }}
                            >
                                {v === 'PUBLIC' ? '🌐 Public — visible to all departments' : '🔒 Private — department only'}
                            </button>
                        ))}
                    </div>
                </div>
                {visibility === 'PRIVATE' && (
                    <div>
                        <label style={{ display: 'block', fontSize: 11, fontWeight: 500, color: '#374151', marginBottom: 4 }}>
                            Department Access
                        </label>
                        {isApprover ? (
                            <MultiSelectDept
                                options={availableDepts}
                                selected={selectedDepts}
                                onChange={setSelectedDepts}
                            />
                        ) : (
                            <div style={{
                                display: 'flex', alignItems: 'center', gap: 8,
                                padding: '8px 12px',
                                background: '#eff6ff', border: '1px solid #bfdbfe',
                                borderRadius: 4, fontSize: 11, color: '#1d4ed8',
                            }}>
                                <span>🔒</span>
                                <span>Restricted to your department: <strong>{userDept || 'unknown'}</strong></span>
                            </div>
                        )}
                    </div>
                )}
            </div>

            {/* Warn-on-attempt hint. Not a blocker — matches sidebar UX. */}
            {missingHints.length > 0 && (
                <div style={{
                    padding: '8px 12px',
                    background: '#fefce8',
                    border: '1px solid #fde68a',
                    borderRadius: 4,
                    fontSize: 11,
                    color: '#92400e',
                }}>
                    Set {missingHints.join(' and ')} before uploading.
                </div>
            )}

            {/* Drop zone — always enabled. */}
            <div
                ref={dropRef}
                onClick={() => fileInputRef.current?.click()}
                style={{
                    border: `2px dashed ${isDragging ? '#9ca3af' : '#d1d5db'}`,
                    borderRadius: 8,
                    padding: 32,
                    textAlign: 'center',
                    background: isDragging ? '#f9fafb' : 'transparent',
                    cursor: 'pointer',
                    transition: 'background 0.15s, border-color 0.15s',
                }}
            >
                <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    style={{ display: 'none' }}
                    accept=".pdf,.docx,.md,.ppt,.pptx,.html,.txt,.xlsx,.xls,.csv"
                    onChange={(e) => {
                        const selected = Array.from(e.target.files);
                        e.target.value = '';
                        ingestFiles(selected);
                    }}
                />
                <div style={{ fontSize: 24, color: '#d1d5db', marginBottom: 6 }}>⬆</div>
                <div style={{ fontSize: 13, color: '#6b7280', fontWeight: 500 }}>
                    Drag &amp; drop files, or click to browse
                </div>
                <div style={{ fontSize: 11, color: '#9ca3af', marginTop: 4 }}>
                    Maximum file size: 25 MB · up to {MAX_FILES_PER_BATCH} files · uploads go through the standard approval queue
                </div>
                <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', justifyContent: 'center', gap: 4 }}>
                    {SUPPORTED_TYPES.map(t => (
                        <span key={t} style={{
                            background: '#f3f4f6', color: '#9ca3af',
                            fontSize: 10, padding: '2px 8px', borderRadius: 3,
                        }}>{t}</span>
                    ))}
                </div>
            </div>

            {uploads.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    {uploads.map(u => (
                        <UploadProgress
                            key={u.id}
                            record={u}
                            onDismiss={() => dismissRecord(u.id)}
                        />
                    ))}
                </div>
            )}

            {uploads.length > 0 && (() => {
                const capReached = uploads.length >= MAX_FILES_PER_BATCH;
                const addDisabled = isUploading || capReached;
                return (
                    <button
                        type="button"
                        onClick={() => { if (!addDisabled) fileInputRef.current?.click(); }}
                        disabled={addDisabled}
                        style={{
                            alignSelf: 'flex-start',
                            padding: '8px 14px',
                            fontSize: 12,
                            fontWeight: 500,
                            color: addDisabled ? '#9ca3af' : '#4f46e5',
                            background: 'white',
                            border: `1px dashed ${addDisabled ? '#e5e7eb' : '#c7d2fe'}`,
                            borderRadius: 6,
                            cursor: addDisabled ? 'not-allowed' : 'pointer',
                        }}
                        title={
                            capReached
                                ? `At most ${MAX_FILES_PER_BATCH} documents per batch — remove one to add another`
                                : isUploading
                                    ? 'Wait for current uploads to finish'
                                    : 'Attach another document'
                        }
                    >+ Add another document</button>
                );
            })()}

            {error && (
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, padding: '10px 14px', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 6 }}>
                    <div style={{ color: '#ef4444', flexShrink: 0 }}>×</div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 500, color: '#b91c1c' }}>Upload Error</div>
                        <div style={{ fontSize: 13, color: '#dc2626', marginTop: 2 }}>{error}</div>
                    </div>
                    <button
                        type="button"
                        onClick={() => setError(null)}
                        style={{
                            padding: '4px 10px', fontSize: 11, fontWeight: 500,
                            color: '#dc2626', background: 'white',
                            border: '1px solid #fca5a5', borderRadius: 3,
                            cursor: 'pointer', flexShrink: 0,
                        }}
                    >Close</button>
                </div>
            )}
        </div>
    );
}
