/**
 * KbScopeCascade — Agent Studio's cascading scope picker.
 *
 * Purpose-built for Agent Studio's Knowledge section. Mirrors the taxonomy
 * the sidebar Knowledge Base GRAPH renders (Domain → Product → Spec
 * Version → Document), but as a plain dropdown form — no graph, no canvas.
 *
 * Design rules the user asked for:
 *   • Domain is MANDATORY.
 *   • Selecting a domain narrows Product to ONLY products that already
 *     have an ACTIVE doc in that domain (not the full /products list).
 *   • Selecting a product narrows Spec Version to versions that exist
 *     under (domain, product).
 *   • Selecting a version narrows Document to files that exist under
 *     (domain, product, version).
 *   • On upload (`includeUploadFields=true`) Source Type is offered as
 *     a free choice from the same 7-value enum the store checks against.
 *
 * Data source: a single `/kb?status=ACTIVE&limit=10000` fetch. All cascade
 * options are derived client-side from the returned docs list, exactly
 * the way KbScopeGraph.jsx builds its tree. Product names are looked up
 * from `/products` so labels stay readable when a doc only carries a
 * `product_id`. Domain list comes from `/products/departments`.
 *
 * Value shape (identical to the previous ScopePicker) so the rest of
 * KnowledgeSection does not have to change:
 *   { domain, product_id, spec_version, source_type?, kb_doc_id?,
 *     product_name?, doc_name?, version_date?, deprecate_prior? }
 *
 * Props:
 *   value                — current scope object (defaults to empty).
 *   onChange             — receives the full merged next value.
 *   includeDocPicker     — render the Document dropdown (existing-KB path).
 *   includeUploadFields  — render Source Type + Version Date + deprecate_prior
 *                          (add-KB path). Spec Version becomes a free-text
 *                          input in this mode (uploading a NEW version).
 *   disabled             — greys the whole form out.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { kbFetch, platformFetch } from '../../config/api';
import ScopeSelect from './ScopeSelect';
import PortalMenu from './PortalMenu';

const SELECT_STYLE = {
    WebkitAppearance: 'none',
    MozAppearance: 'none',
    appearance: 'none',
    backgroundImage: 'none',
    width: '100%',
    background: 'white',
    border: '1px solid #e5e7eb',
    borderRadius: 6,
    padding: '8px 32px 8px 12px',
    fontSize: 12,
    color: '#374151',
    outline: 'none',
    cursor: 'pointer',
};

const INPUT_STYLE = {
    width: '100%',
    background: 'white',
    border: '1px solid #e5e7eb',
    borderRadius: 6,
    padding: '8px 12px',
    fontSize: 12,
    color: '#374151',
    outline: 'none',
};

const LABEL_STYLE = {
    display: 'block',
    fontSize: 11,
    fontWeight: 500,
    color: '#374151',
    marginBottom: 6,
};

// Mirrors the DB CHECK on knowledge_docs.source_type — same enum the sidebar
// KB uploader shows. Kept locally so a future divergence between the two
// surfaces surfaces at review time, not at runtime.
const SOURCE_TYPES = [
    { value: 'BRD',           label: 'BRD — Business Requirements' },
    { value: 'FSD',           label: 'FSD — Functional Spec' },
    { value: 'TPMC_DECISION', label: 'TPMC Decision' },
    { value: 'RBI_CIRCULAR',  label: 'RBI Circular' },
    { value: 'ARCHITECTURE',  label: 'Architecture / Design' },
    { value: 'SPEC',          label: 'Spec' },
    { value: 'OTHER',         label: 'Other' },
];

function SelectChevron() {
    return (
        <svg
            aria-hidden="true"
            width="14"
            height="14"
            viewBox="0 0 20 20"
            fill="none"
            style={{
                position: 'absolute',
                right: 10,
                top: '50%',
                transform: 'translateY(-50%)',
                pointerEvents: 'none',
                color: '#9ca3af',
            }}
        >
            <path d="M6 8l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
    );
}

export default function KbScopeCascade({
    value = {},
    onChange,
    includeDocPicker = false,
    includeUploadFields = false,
    disabled = false,
}) {
    const [docs, setDocs] = useState([]);          // full /kb?status=ACTIVE list
    const [docsLoaded, setDocsLoaded] = useState(false);
    const [departments, setDepartments] = useState([]);
    const [productNames, setProductNames] = useState(new Map()); // id -> name

    // Domain type-ahead — same UX as the sidebar's ScopePicker so users
    // recognise the interaction, minus the "select any domain" freedom
    // (only domains that actually have docs are surfaced, matching the
    // graph's behaviour).
    const [deptOpen, setDeptOpen] = useState(false);
    const [deptInput, setDeptInput] = useState(value.domain || '');
    // `deptTyping` distinguishes "user is actively editing the field" from
    // "field is just displaying the committed domain". While typing we keep
    // the raw keystrokes in deptInput and DO NOT let the value→input sync
    // effect clobber them; when not typing the field mirrors value.domain.
    const [deptTyping, setDeptTyping] = useState(false);
    const deptRef = useRef(null);

    // Keep the visible text in sync with the committed domain, but never while
    // the user is mid-edit (otherwise their keystrokes get wiped and it feels
    // like the domain can't be changed after a product is picked).
    useEffect(() => {
        if (deptTyping) return;
        setDeptInput(value.domain || '');
    }, [value.domain, deptTyping]);

    // Outside-click handling for the domain menu is delegated to <PortalMenu>
    // (it checks both the anchor and the portaled menu), so no local
    // document mousedown listener is needed here.

    // Load the ACTIVE docs corpus once. Everything cascades off this list.
    useEffect(() => {
        let alive = true;
        (async () => {
            try {
                const res = await kbFetch('?status=ACTIVE&limit=10000');
                if (!res.ok) return;
                const data = await res.json();
                if (!alive) return;
                setDocs(data.docs || data.items || []);
                setDocsLoaded(true);
            } catch { /* non-fatal */ }
        })();
        return () => { alive = false; };
    }, []);

    // Load the department list (matches the sidebar's ScopePicker source).
    // The domain dropdown intersects this with the domains that actually
    // have docs so the user is never offered a dead-end selection.
    useEffect(() => {
        let alive = true;
        (async () => {
            try {
                const res = await platformFetch('/products/departments');
                if (!res.ok) return;
                const data = await res.json();
                if (!alive) return;
                setDepartments((data.departments || []).filter(d => d && d.trim() !== ''));
            } catch { /* non-fatal */ }
        })();
        return () => { alive = false; };
    }, []);

    // Load product id → name so cards / dropdowns render human names
    // instead of raw UUIDs.
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
                setProductNames(map);
            } catch { /* non-fatal */ }
        })();
        return () => { alive = false; };
    }, []);

    // ── Derived cascade lists ──────────────────────────────────────────
    // Domains that actually have ACTIVE docs. Intersected with the
    // /products/departments list so we surface the "official" label case
    // (e.g. "HR" rather than a stray "hr" someone typed once).
    const availableDomains = useMemo(() => {
        const withDocs = new Set();
        for (const d of docs) {
            if (d.domain && d.domain.trim()) withDocs.add(d.domain);
        }
        // Prefer the /products/departments spelling when a case-insensitive
        // match exists; otherwise fall back to the raw doc.domain value.
        const officialByLower = new Map(
            departments.map(x => [x.toLowerCase(), x])
        );
        const merged = new Set();
        for (const d of withDocs) merged.add(officialByLower.get(d.toLowerCase()) || d);
        return Array.from(merged).sort((a, b) => a.localeCompare(b));
    }, [docs, departments]);

    // Products under the currently-selected domain.
    const availableProducts = useMemo(() => {
        if (!value.domain) return [];
        const domainLower = String(value.domain).toLowerCase();
        const ids = new Set();
        for (const d of docs) {
            if (!d.product_id) continue;
            if ((d.domain || '').toLowerCase() !== domainLower) continue;
            ids.add(String(d.product_id));
        }
        return Array.from(ids)
            .map(id => ({ id, name: productNames.get(id) || id }))
            .sort((a, b) => a.name.localeCompare(b.name));
    }, [docs, value.domain, productNames]);

    // Spec versions under (domain, product).
    const availableVersions = useMemo(() => {
        if (!value.domain || !value.product_id) return [];
        const domainLower = String(value.domain).toLowerCase();
        const versions = new Set();
        for (const d of docs) {
            if (!d.product_id || !d.spec_version) continue;
            if ((d.domain || '').toLowerCase() !== domainLower) continue;
            if (String(d.product_id) !== String(value.product_id)) continue;
            versions.add(d.spec_version);
        }
        return Array.from(versions).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    }, [docs, value.domain, value.product_id]);

    // Docs under (domain, product, version). Version is optional — when
    // the user has not picked a version the doc dropdown is disabled so
    // the cascade stays strict.
    const availableDocs = useMemo(() => {
        if (!value.domain || !value.product_id || !value.spec_version) return [];
        const domainLower = String(value.domain).toLowerCase();
        return docs
            .filter(d =>
                (d.domain || '').toLowerCase() === domainLower
                && String(d.product_id) === String(value.product_id)
                && d.spec_version === value.spec_version
            )
            .sort((a, b) => {
                const an = (a.name || a.display_name || a.id || '').toLowerCase();
                const bn = (b.name || b.display_name || b.id || '').toLowerCase();
                return an.localeCompare(bn);
            });
    }, [docs, value.domain, value.product_id, value.spec_version]);

    // ── Change handlers with cascade invalidation ──────────────────────
    // Each level clears every child level so the picker never shows a
    // combination that has no docs behind it.
    const merge = (patch) => onChange?.({ ...value, ...patch });

    function onDomainChange(next) {
        if ((next || '') === (value.domain || '')) return;
        merge({
            domain: next || null,
            product_id: null,
            product_name: null,
            spec_version: null,
            kb_doc_id: null,
            doc_name: null,
        });
    }

    function onProductChange(pid) {
        const name = pid ? (productNames.get(pid) || '') : null;
        merge({
            product_id: pid || null,
            product_name: name,
            spec_version: null,
            kb_doc_id: null,
            doc_name: null,
        });
    }

    function onVersionChange(v) {
        merge({
            spec_version: v || null,
            kb_doc_id: null,
            doc_name: null,
        });
    }

    function onDocChange(docId) {
        const doc = availableDocs.find(d => String(d.id) === String(docId));
        merge({
            kb_doc_id: docId || null,
            doc_name: doc ? (doc.name || doc.display_name || '') : null,
        });
    }

    // Filtered domain type-ahead list.
    const filteredDomains = availableDomains.filter(d =>
        d.toLowerCase().includes(deptInput.toLowerCase())
    );

    // Empty-state hints — the user asked us to leave no ambiguity about
    // WHY a downstream dropdown is disabled.
    const productHint =
        !value.domain ? 'Select a domain first'
        : (docsLoaded && availableProducts.length === 0) ? 'No products with docs under this domain'
        : '';
    const versionHint =
        !value.product_id ? 'Select a product first'
        : (docsLoaded && availableVersions.length === 0) ? 'No versions yet for this product'
        : '';
    const docHint =
        !value.spec_version ? 'Select a version first'
        : (docsLoaded && availableDocs.length === 0) ? 'No documents under this version'
        : '';

    return (
        <div
            style={{
                display: 'flex',
                flexDirection: 'column',
                gap: 12,
                opacity: disabled ? 0.5 : 1,
                pointerEvents: disabled ? 'none' : 'auto',
            }}
        >
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 12 }}>
                {/* Domain (MANDATORY) */}
                <div>
                    <label style={LABEL_STYLE}>
                        Domain <span style={{ color: '#f43f5e' }} aria-hidden="true">*</span>
                    </label>
                    <div ref={deptRef} style={{ position: 'relative' }}>
                        <input
                            type="text"
                            value={deptInput}
                            onChange={e => {
                                // Pure filter — typing NEVER commits or clears the
                                // domain. Commit happens only on option click below,
                                // so the user can freely retype to pick another domain
                                // (which then resets product/version/doc).
                                setDeptTyping(true);
                                setDeptInput(e.target.value);
                                setDeptOpen(true);
                            }}
                            onFocus={() => {
                                // Open on focus and clear the visible text so the full
                                // list is shown, letting the user pick a DIFFERENT
                                // domain even after a product was already selected.
                                setDeptTyping(true);
                                setDeptInput('');
                                setDeptOpen(true);
                            }}
                            onClick={() => {
                                setDeptTyping(true);
                                setDeptOpen(true);
                            }}
                            onBlur={() => {
                                // Leaving the field without picking anything: stop
                                // typing mode so the field snaps back to the committed
                                // domain (handled by the sync effect).
                                setDeptTyping(false);
                            }}
                            placeholder={value.domain || 'Select domain'}
                            aria-required="true"
                            autoComplete="off"
                            style={{
                                ...SELECT_STYLE,
                                color: value.domain ? '#374151' : '#9ca3af',
                            }}
                        />
                        <SelectChevron />
                        <PortalMenu
                            anchorRef={deptRef}
                            open={deptOpen}
                            onRequestClose={() => { setDeptOpen(false); setDeptTyping(false); }}
                        >
                            {filteredDomains.length === 0 && (
                                <div className="kb-scope-menu__empty">
                                    {docsLoaded ? 'No domains with docs' : 'Loading…'}
                                </div>
                            )}
                            {filteredDomains.map(dept => (
                                <div
                                    key={dept}
                                    role="option"
                                    aria-selected={value.domain === dept}
                                    className={
                                        'kb-scope-menu__item'
                                        + (value.domain === dept ? ' kb-scope-menu__item--active' : '')
                                    }
                                    // onMouseDown + preventDefault keeps input focus so
                                    // the pick lands before the input's onBlur fires.
                                    onMouseDown={(e) => {
                                        e.preventDefault();
                                        setDeptTyping(false);
                                        setDeptInput(dept);
                                        onDomainChange(dept);   // resets product/version/doc
                                        setDeptOpen(false);
                                    }}
                                >
                                    {dept}
                                </div>
                            ))}
                        </PortalMenu>
                    </div>
                </div>

                {/* Product (cascades from domain) */}
                <div>
                    <label style={LABEL_STYLE}>
                        Product <span style={{ color: '#f43f5e' }} aria-hidden="true">*</span>
                    </label>
                    <ScopeSelect
                        value={value.product_id || ''}
                        onChange={(v) => onProductChange(v)}
                        disabled={!value.domain || availableProducts.length === 0}
                        placeholder={productHint || 'Select product'}
                        emptyText="No products with docs under this domain"
                        options={availableProducts.map(p => ({ value: p.id, label: p.name }))}
                    />
                    {productHint && (
                        <div style={{ marginTop: 4, fontSize: 10, color: '#9ca3af' }}>{productHint}</div>
                    )}
                </div>

                {/* Spec Version. In upload mode this is free-text (uploading a
                    NEW version); otherwise it's a dropdown of existing versions. */}
                <div>
                    <label style={LABEL_STYLE}>
                        Spec Version <span style={{ color: '#f43f5e' }} aria-hidden="true">*</span>
                    </label>
                    {includeUploadFields ? (
                        <input
                            type="text"
                            value={value.spec_version || ''}
                            onChange={e => onVersionChange(e.target.value)}
                            placeholder="e.g. v3, 2025.1"
                            disabled={!value.product_id}
                            style={{
                                ...INPUT_STYLE,
                                color: value.spec_version ? '#374151' : '#9ca3af',
                            }}
                        />
                    ) : (
                        <ScopeSelect
                            value={value.spec_version || ''}
                            onChange={(v) => onVersionChange(v)}
                            disabled={!value.product_id || availableVersions.length === 0}
                            placeholder={versionHint || 'Select version'}
                            emptyText="No versions yet for this product"
                            options={availableVersions.map(v => ({ value: v, label: v }))}
                        />
                    )}
                    {versionHint && !includeUploadFields && (
                        <div style={{ marginTop: 4, fontSize: 10, color: '#9ca3af' }}>{versionHint}</div>
                    )}
                </div>

                {/* Version Date — upload only, optional */}
                {includeUploadFields && (
                    <div>
                        <label style={LABEL_STYLE}>
                            Version Date <span style={{ color: '#9ca3af', fontWeight: 400 }}>(optional)</span>
                        </label>
                        <input
                            type="date"
                            value={value.version_date || ''}
                            onChange={e => merge({ version_date: e.target.value || null })}
                            style={{
                                ...INPUT_STYLE,
                                color: value.version_date ? '#374151' : '#9ca3af',
                            }}
                        />
                    </div>
                )}

                {/* Source Type — upload only, optional */}
                {includeUploadFields && (
                    <div>
                        <label style={LABEL_STYLE}>
                            Source Type <span style={{ color: '#9ca3af', fontWeight: 400 }}>(optional)</span>
                        </label>
                        <ScopeSelect
                            value={value.source_type || ''}
                            onChange={(v) => merge({ source_type: v || null })}
                            placeholder="Select source type"
                            options={SOURCE_TYPES.map(t => ({ value: t.value, label: t.label }))}
                        />
                    </div>
                )}
            </div>

            {/* Document picker — narrows further to a single specific doc. */}
            {includeDocPicker && (
                <div>
                    <label style={LABEL_STYLE}>
                        Document <span style={{ color: '#9ca3af', fontWeight: 400 }}>(optional)</span>
                    </label>
                    <ScopeSelect
                        value={value.kb_doc_id || ''}
                        onChange={(v) => onDocChange(v)}
                        disabled={!value.spec_version || availableDocs.length === 0}
                        placeholder={docHint || '— Any document in this scope —'}
                        allowEmpty
                        emptyLabel="— Any document in this scope —"
                        emptyText="No documents under this version"
                        options={availableDocs.map(d => ({
                            value: d.id,
                            label: (d.name || d.display_name || d.id),
                        }))}
                    />
                    {docHint && (
                        <div style={{ marginTop: 4, fontSize: 10, color: '#9ca3af' }}>{docHint}</div>
                    )}
                </div>
            )}

            {/* Deprecate-prior — upload only, requires product + domain */}
            {includeUploadFields && value.product_id && value.domain && (
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', userSelect: 'none' }}>
                    <input
                        type="checkbox"
                        checked={!!value.deprecate_prior}
                        onChange={e => merge({ deprecate_prior: e.target.checked })}
                        style={{ accentColor: '#4f46e5' }}
                    />
                    <span style={{ fontSize: 12, color: '#4b5563' }}>
                        Deprecate prior versions of this product + domain on approval
                    </span>
                </label>
            )}
        </div>
    );
}
