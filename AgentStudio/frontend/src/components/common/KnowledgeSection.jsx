// SPDX-License-Identifier: MIT
/**
 * KnowledgeSection — Build Studio's "Knowledge" attachment control.
 *
 * Used by AgentEditor.jsx and the workflow editor's ConfigPanel.jsx to let
 * users attach a knowledge corpus to an agent. Three modes:
 *
 *   • None         → no RAG retrieval at runtime.
 *   • Existing KB  → at runtime, AgentRunner retrieves from the platform's
 *                    pgvector KB (PUBLIC + owner-dept PRIVATE docs). The UI
 *                    is a single inline cascade (Domain → Product → Spec
 *                    Version → optional Document). As soon as the tuple is
 *                    complete the scope is auto-persisted onto
 *                    `knowledge.scopes[0]`. There is no separate "attach"
 *                    step and no card list — the cascade IS the picker.
 *   • Add KB       → uploads new docs through the platform's standard
 *                    /kb/upload endpoint (same route the sidebar Knowledge
 *                    Base uses, PENDING_APPROVAL queue). The user picks
 *                    the graph scope FIRST — Domain / Product / Spec
 *                    Version / Source Type — and every uploaded file is
 *                    tagged with those fields, so it lands in the same
 *                    bucket as sidebar KB uploads.
 *
 * Design constraint (see plan §0): NO graph / canvas / force-directed UI
 * is added to Agent Studio. This section is purely a dropdown form. The
 * graph stays in the main sidebar Knowledge Base.
 *
 * The `value` prop is the agent.knowledge JSONB blob:
 *
 *   {
 *     mode: 'none' | 'existing_kb' | 'add_kb',
 *
 *     // NEW graph model — retrieval reads this first when present:
 *     scopes: [
 *       { domain, product_id, spec_version?, source_type?, kb_doc_id?,
 *         product_name?, doc_name? }        // *_name fields are display-only
 *     ],
 *
 *     // LEGACY — kept intact so pre-migration agents keep retrieving:
 *     namespaces: string[],
 *     selected_doc_ids?: string[],
 *     full_file_doc_ids?: string[],
 *
 *     // Add-KB provenance:
 *     uploaded_doc_ids?: string[]
 *   }
 *
 * Props:
 *   value        — current agent.knowledge object (defaults to { mode: 'none' })
 *   onChange     — called with the next knowledge object on any user action
 *   userDept     — current user's department. Forwarded to
 *                  KnowledgeUploadInline so non-approver uploads default to
 *                  Private-locked-to-your-dept, matching the sidebar KB.
 *   isApprover   — when true, KnowledgeUploadInline renders the multi-
 *                  department select on Private uploads (same rule the
 *                  sidebar's own Visibility panel uses).
 *   isAdmin      — when true (role === 'admin'), the Add-KB Domain picker is a
 *                  free choice over every department; otherwise it is locked to
 *                  the user's own department (userDept).
 */
import { useEffect, useState } from 'react';
import { kbFetch, platformFetch } from '../../config/api';
import KnowledgeUploadInline from './KnowledgeUploadInline';
import KbScopeCascade from './KbScopeCascade';
import KbUploadScopePicker from './KbUploadScopePicker';

// Canonical values of agent.knowledge.mode. Keep these in lockstep with
// ABStudio/backend/app/core/kb_retriever.py::KB_MODE_* — the JSONB blob
// crosses the wire so the spellings must match exactly.
export const KB_MODE_NONE = 'none';
export const KB_MODE_EXISTING = 'existing_kb';
export const KB_MODE_ADD = 'add_kb';

const MODES = [
    {
        value: KB_MODE_NONE,
        label: 'None',
        hint: 'Agent answers from its own model only — no knowledge retrieval.',
    },
    {
        value: KB_MODE_EXISTING,
        label: 'Existing Knowledge Bases',
        hint: 'Pick a slice of the platform KB by Domain → Product → Spec Version → (optional Document).',
    },
    {
        value: KB_MODE_ADD,
        label: 'Add Knowledge Base',
        hint: 'Upload new documents into a scope. They are immediately available to the workflow.',
    },
];

// Deterministic scope-card key. Same shape used server-side to dedupe.
function _scopeKey(s) {
    if (!s) return '';
    return [
        s.product_id || '',
        s.domain || '',
        s.spec_version || '',
        s.source_type || '',
        s.kb_doc_id || '',
    ].join('|');
}

function _scopesEqual(a, b) {
    return _scopeKey(a) === _scopeKey(b);
}

/**
 * @param {{
 *   value?: {
 *     mode: string,
 *     scopes?: Array<{
 *       domain: string, product_id: string, spec_version?: string,
 *       source_type?: string, kb_doc_id?: string,
 *       product_name?: string, doc_name?: string
 *     }>,
 *     namespaces?: string[],
 *     uploaded_doc_ids?: string[],
 *     full_file_doc_ids?: string[],
 *     selected_doc_ids?: string[],
 *   },
 *   onChange?: (next: object) => void,
 *   userDept?: string,
 *   isApprover?: boolean,
 *   isAdmin?: boolean,
 * }} props
 */
export default function KnowledgeSection({
    value = { mode: KB_MODE_NONE },
    onChange = () => {},
    userDept = '',
    isApprover = false,
    isAdmin = false,
}) {
    const mode = (value && value.mode) || KB_MODE_NONE;
    const scopes = (value && Array.isArray(value.scopes)) ? value.scopes : [];
    const namespaces = (value && Array.isArray(value.namespaces)) ? value.namespaces : [];
    const uploadedDocIds = (value && Array.isArray(value.uploaded_doc_ids)) ? value.uploaded_doc_ids : [];

    // Existing-KB mode uses a single inline cascade. The active scope is
    // just the first (and only) entry of `value.scopes[]`. When any of
    // Domain / Product / Spec Version is unset we keep the partial value
    // in local state so the cascade renders correctly, but we do NOT
    // persist a half-filled scope onto the agent — the retriever would
    // otherwise treat an entry with no product_id as "any product", which
    // is the opposite of what the user wants.
    const initialScope = (Array.isArray(value?.scopes) && value.scopes[0]) || {};
    const [activeScope, setActiveScopeLocal] = useState(initialScope);

    // Add-KB mode's scope drives both the upload metadata and the /kb query
    // for the doc picker. Same shape as the KbScopeCascade value.
    const [uploadScope, setUploadScope] = useState({});

    // Product name lookup used by scope labels and legacy conversion.
    const [productsById, setProductsById] = useState(new Map());

    // Legacy migration state — only computed when the blob still uses
    // `namespaces` and has no `scopes` yet.
    const [migrationDismissed, setMigrationDismissed] = useState(false);
    const hasLegacyOnly = namespaces.length > 0 && scopes.length === 0;

    // Fetch product map once so cards can render product names when the
    // stored scope only carries product_id.
    useEffect(() => {
        let alive = true;
        (async () => {
            try {
                const res = await platformFetch('/products?limit=200');
                if (!res.ok) return;
                const data = await res.json();
                if (!alive) return;
                const map = new Map();
                (data.products || data.items || []).forEach(p => {
                    if (p && p.id) map.set(String(p.id), p.name || p.id);
                });
                setProductsById(map);
            } catch { /* non-fatal */ }
        })();
        return () => { alive = false; };
    }, []);

    function setMode(next) {
        if (next === mode) return;
        if (next === KB_MODE_NONE) {
            onChange({ mode: KB_MODE_NONE });
            return;
        }
        // Preserve scopes / uploaded_doc_ids / legacy namespaces on the way
        // through so toggling between Existing and Add KB never loses the
        // user's attachments.
        onChange({
            ...value,
            mode: next,
            scopes,
            namespaces,
            uploaded_doc_ids: uploadedDocIds,
        });
    }

    // A scope is "complete" — and therefore worth persisting — only when
    // Domain + Product + Spec Version are all set. Anything less would make
    // the retriever fall back to a broader corpus, which contradicts what
    // the user just did in the picker.
    function isScopeComplete(s) {
        return !!(s && s.product_id && s.domain && (s.spec_version || '').trim());
    }

    // Local-state wrapper: always keep the cascade in sync, but only push
    // to `value.scopes` when the tuple is complete. When the user reduces
    // the selection (e.g. clears Product), we clear the persisted scope so
    // the retriever doesn't keep a stale filter alive.
    function setActiveScope(next) {
        const enriched = next && next.product_id
            ? { ...next, product_name: productsById.get(String(next.product_id)) || next.product_name || '' }
            : (next || {});
        setActiveScopeLocal(enriched);

        if (isScopeComplete(enriched)) {
            const current = (Array.isArray(value?.scopes) && value.scopes[0]) || null;
            // Skip the onChange when the persisted scope is byte-identical
            // to what we're about to write; prevents a save loop under
            // parents that re-emit `value` on every render.
            if (current && _scopesEqual(current, enriched)) return;
            onChange({
                ...value,
                mode: KB_MODE_EXISTING,
                scopes: [enriched],
            });
        } else if (Array.isArray(value?.scopes) && value.scopes.length > 0) {
            // Persisted scope must be cleared when the user backs out of a
            // complete selection — otherwise the retriever would keep
            // filtering by a scope the UI no longer shows.
            onChange({
                ...value,
                mode: KB_MODE_EXISTING,
                scopes: [],
            });
        }
    }

    // Client-side conversion of legacy { namespaces: [...] } to the graph
    // model. Fetches ACTIVE docs in each namespace, groups by
    // (domain, product_id, spec_version), and seeds one scope per group.
    // Any namespace whose docs share exactly one (domain, product_id)
    // still yields a valid scope; ambiguous namespaces surface a warning
    // and are left in `namespaces` for the retriever's legacy path to
    // handle.
    async function convertLegacy() {
        try {
            const qs = new URLSearchParams({ status: 'ACTIVE', limit: '10000' });
            const res = await kbFetch(`?${qs.toString()}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const docs = data.docs || data.items || [];
            const nsSet = new Set(namespaces);
            const groups = new Map(); // scopeKey -> scope object
            const remainingNs = new Set();
            for (const d of docs) {
                if (!nsSet.has(d.namespace)) continue;
                if (!d.product_id || !d.domain) {
                    // Uncategorised legacy doc — cannot express in graph model.
                    remainingNs.add(d.namespace);
                    continue;
                }
                const scope = {
                    domain: d.domain,
                    product_id: String(d.product_id),
                    spec_version: d.spec_version || '',
                    source_type: '',
                    kb_doc_id: '',
                    product_name: productsById.get(String(d.product_id)) || '',
                };
                const key = _scopeKey(scope);
                if (!groups.has(key)) groups.set(key, scope);
            }
            onChange({
                ...value,
                mode: KB_MODE_EXISTING,
                scopes: Array.from(groups.values()),
                // Keep only namespaces we could not convert so the retriever
                // legacy path still resolves them.
                namespaces: Array.from(remainingNs),
            });
        } catch {
            // Non-fatal: on failure we simply leave the legacy blob in place.
        }
    }

    function handleUploaded(result) {
        if (!result || !result.doc_id) return;
        // De-dupe uploaded_doc_ids.
        const nextDocIds = Array.from(new Set([...uploadedDocIds, result.doc_id])).filter(Boolean);
        // Also register a pinned scope for the uploaded doc so retrieval
        // after approval is scoped to exactly this file. When the upload
        // was tagged with product/domain/spec_version, use those.
        const pinned = {
            domain:       result.domain       || uploadScope.domain       || '',
            product_id:   result.product_id   || uploadScope.product_id   || '',
            spec_version: result.spec_version || uploadScope.spec_version || '',
            source_type:  result.source_type  || uploadScope.source_type  || '',
            kb_doc_id:    result.doc_id,
            product_name: productsById.get(String(result.product_id || uploadScope.product_id)) || '',
            doc_name:     result.filename || '',
        };
        const nextScopes = scopes.some(s => _scopesEqual(s, pinned))
            ? scopes
            : [...scopes, pinned];
        // Backfill legacy `namespaces` so any downstream code still on
        // namespace grouping (or a rollback) keeps working.
        const nextNs = Array.from(new Set([...namespaces, result.namespace])).filter(Boolean);
        onChange({
            ...value,
            mode: KB_MODE_ADD,
            scopes: nextScopes,
            namespaces: nextNs,
            uploaded_doc_ids: nextDocIds,
        });
    }

    // ── Render ─────────────────────────────────────────────────────────
    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {/* Mode radio */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {MODES.map(m => {
                    const checked = mode === m.value;
                    return (
                        <label
                            key={m.value}
                            style={{
                                display: 'flex',
                                alignItems: 'flex-start',
                                gap: 10,
                                padding: '10px 12px',
                                borderRadius: 6,
                                border: `1px solid ${checked ? '#4f46e5' : '#e5e7eb'}`,
                                background: checked ? '#eef2ff' : 'white',
                                cursor: 'pointer',
                            }}
                        >
                            <input
                                type="radio"
                                name="knowledge-mode"
                                value={m.value}
                                checked={checked}
                                onChange={() => setMode(m.value)}
                                style={{ marginTop: 3, accentColor: '#4f46e5', cursor: 'pointer' }}
                            />
                            <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontSize: 13, fontWeight: 500, color: '#1f2937' }}>{m.label}</div>
                                <div style={{ fontSize: 11, color: '#6b7280', marginTop: 2 }}>{m.hint}</div>
                            </div>
                        </label>
                    );
                })}
            </div>

            {/* Existing KB — inline cascade. The user picks Domain →
                Product → Spec Version (→ optional Document) directly; the
                selection is auto-persisted onto knowledge.scopes on every
                change. No card list, no "+ Add Knowledge Scope" button,
                no draft/attach round-trip — the cascade IS the picker. */}
            {mode === KB_MODE_EXISTING && (
                <div style={{
                    border: '1px solid #e5e7eb', borderRadius: 6, padding: 12,
                    background: '#fafafa',
                    display: 'flex', flexDirection: 'column', gap: 10,
                }}>
                    <div style={{ fontSize: 11, fontWeight: 500, color: '#374151' }}>
                        Knowledge Scope
                    </div>

                    {/* Legacy migration banner — surfaces only when the
                        stored blob still uses `namespaces` and has not been
                        converted yet. Kept because pre-migration agents
                        still exist in the wild. */}
                    {hasLegacyOnly && !migrationDismissed && (
                        <div style={{
                            padding: '8px 12px',
                            background: '#fefce8',
                            border: '1px solid #fde68a',
                            borderRadius: 4,
                            fontSize: 11,
                            color: '#92400e',
                            display: 'flex', alignItems: 'center', gap: 8,
                        }}>
                            <span style={{ flex: 1 }}>
                                Legacy namespace attachment: <strong>{namespaces.join(', ')}</strong>.
                                Convert to the graph model so this agent stays in step with the sidebar KB.
                            </span>
                            <button
                                type="button"
                                onClick={convertLegacy}
                                style={{
                                    padding: '4px 10px',
                                    fontSize: 11,
                                    fontWeight: 600,
                                    borderRadius: 4,
                                    background: '#f59e0b',
                                    color: 'white',
                                    border: 'none',
                                    cursor: 'pointer',
                                }}
                            >Convert</button>
                            <button
                                type="button"
                                onClick={() => setMigrationDismissed(true)}
                                style={{
                                    padding: '4px 8px',
                                    fontSize: 11,
                                    background: 'none',
                                    border: 'none',
                                    color: '#92400e',
                                    cursor: 'pointer',
                                }}
                                aria-label="Dismiss"
                            >×</button>
                        </div>
                    )}

                    <KbScopeCascade
                        value={activeScope}
                        onChange={setActiveScope}
                        includeDocPicker={true}
                        includeUploadFields={false}
                    />

                    {/* Mandatory-fields hint. Fires only after the user has
                        started filling the cascade so a freshly-toggled
                        "Existing KB" mode doesn't scream at them from the
                        first paint. */}
                    {(activeScope.domain || activeScope.product_id || activeScope.spec_version) && !isScopeComplete(activeScope) && (
                        <div style={{
                            padding: '6px 10px',
                            fontSize: 11,
                            fontWeight: 500,
                            color: '#b91c1c',
                            background: '#fef2f2',
                            border: '1px solid #fecaca',
                            borderRadius: 4,
                        }}>
                            Pick Domain, Product, and Spec Version to activate this scope.
                        </div>
                    )}
                </div>
            )}

            {/* Add KB — sidebar-parity scope picker + inline uploader.
                Uses KbUploadScopePicker (not KbScopeCascade) so all products
                and all domains are pickable — the user may be uploading the
                first doc for a fresh (domain, product, version) tuple that
                does not yet exist in the KB. */}
            {mode === KB_MODE_ADD && (
                <div style={{
                    border: '1px solid #e5e7eb', borderRadius: 6, padding: 14,
                    background: '#fafafa',
                    display: 'flex', flexDirection: 'column', gap: 12,
                }}>
                    <div style={{ fontSize: 11, fontWeight: 500, color: '#374151' }}>
                        Classify the upload
                    </div>
                    <KbUploadScopePicker
                        value={uploadScope}
                        onChange={setUploadScope}
                        userDept={userDept}
                        isAdmin={isAdmin}
                    />

                    {uploadedDocIds.length > 0 && (
                        <div style={{
                            padding: '8px 12px',
                            background: '#ecfdf5',
                            border: '1px solid #a7f3d0',
                            borderRadius: 4,
                            fontSize: 11,
                            color: '#047857',
                        }}>
                            ✓ {uploadedDocIds.length} document{uploadedDocIds.length === 1 ? '' : 's'} submitted for approval.
                            {' '}They will be searchable once an approver signs off in the Inbox.
                        </div>
                    )}
                    <KnowledgeUploadInline
                        scope={uploadScope}
                        userDept={userDept}
                        isApprover={isApprover}
                        onUploaded={handleUploaded}
                    />
                </div>
            )}
        </div>
    );
}
