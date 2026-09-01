/**
 * KbUploadScopePicker — flat scope picker used ONLY for the Add-KB upload
 * flow inside Agent Studio.
 *
 * Mirrors ai-ui/src/components/ScopePicker.jsx (the sidebar Knowledge Base
 * page's upload widget) 1:1 in field set: Domain, Product, Spec Version,
 * Version Date, Source Type, plus the "Deprecate prior versions" checkbox.
 *
 * Why this is separate from KbScopeCascade
 * -----------------------------------------
 * KbScopeCascade filters Product / Version / Doc to only those tuples that
 * already have ACTIVE docs in the platform KB — which is the right thing to
 * do when the user is ATTACHING to an existing corpus. It is the wrong thing
 * to do for UPLOADS: you may be uploading the very first doc for a
 * (domain, product, version) tuple, and cascade-filtering would hide the
 * product from the dropdown because it has no docs yet.
 *
 * So Existing-KB uses KbScopeCascade; Add-KB uses this. Same value shape,
 * different source of truth.
 *
 * Value shape:
 *   { product_id, domain, spec_version, version_date?, source_type?,
 *     deprecate_prior?, product_name? }
 *
 * Props:
 *   value      — current scope object.
 *   onChange   — receives the full merged next value.
 *   disabled   — greys the whole form out.
 *   userDept   — current user's department. For non-admins the Domain is
 *                prefilled to and locked at this value (they may only add
 *                knowledge to their own department).
 *   isAdmin    — when true the Domain stays a free type-ahead over every
 *                department; otherwise it is read-only (userDept).
 */
import { useEffect, useRef, useState } from 'react';
import { platformFetch } from '../../config/api';
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

// Kept in lockstep with the sidebar KB uploader's enum (see
// ai-ui/src/components/ScopePicker.jsx). Mirrors the DB CHECK on
// knowledge_docs.source_type.
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

export default function KbUploadScopePicker({
    value = {},
    onChange,
    disabled = false,
    userDept = '',
    isAdmin = false,
}) {
    const [products, setProducts] = useState([]);
    const [productsLoaded, setProductsLoaded] = useState(false);
    const [departments, setDepartments] = useState([]);

    // Domain type-ahead — identical UX to the sidebar's picker.
    const [deptOpen, setDeptOpen] = useState(false);
    const [deptInput, setDeptInput] = useState(value.domain || '');
    // Same "actively editing vs displaying committed value" guard as
    // KbScopeCascade — prevents the value→input sync from wiping keystrokes
    // mid-edit (which made the domain feel unchangeable after a pick).
    const [deptTyping, setDeptTyping] = useState(false);
    const deptRef = useRef(null);

    useEffect(() => {
        if (deptTyping) return;
        setDeptInput(value.domain || '');
    }, [value.domain, deptTyping]);

    // Outside-click handling for the domain menu is delegated to <PortalMenu>.

    // Department list — same endpoint the sidebar uses.
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

    // Product list — ACTIVE only (matches the sidebar filter). NOT filtered by
    // domain — the user might be uploading the first doc for a
    // (domain, product) combo that does not yet exist in the KB.
    useEffect(() => {
        let alive = true;
        (async () => {
            try {
                const res = await platformFetch('/products?limit=200');
                if (!res.ok) return;
                const data = await res.json();
                if (!alive) return;
                setProducts(
                    (data.products || data.items || [])
                        .filter(p => (p?.status || 'ACTIVE') === 'ACTIVE')
                );
                setProductsLoaded(true);
            } catch { /* non-fatal */ }
        })();
        return () => { alive = false; };
    }, []);

    // Non-admin uploaders may only add knowledge to their OWN department, so
    // the Domain defaults to (and stays locked at) userDept. Admins keep the
    // free type-ahead over every department. Prefill runs whenever userDept
    // resolves (it starts empty until /auth/me returns) or diverges from the
    // current value so a freshly-mounted picker lands on the correct domain.
    useEffect(() => {
        if (isAdmin) return;
        if (!userDept) return;
        if (value.domain === userDept) return;
        onChange?.({ ...value, domain: userDept });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isAdmin, userDept, value.domain]);

    const merge = (patch) => onChange?.({ ...value, ...patch });

    const onProductChange = (pid) => {
        const p = products.find(x => x.id === pid);
        merge({ product_id: pid || null, product_name: p ? p.name : null });
    };
    const onDomainChange  = (d) => merge({ domain: d || null });
    const onVersionChange = (v) => merge({ spec_version: v || null });

    const filteredDepts = departments.filter(d =>
        d.toLowerCase().includes(deptInput.toLowerCase())
    );
    const hasNoProducts = productsLoaded && products.length === 0;

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
                {/* Domain (Department) — MANDATORY.
                    Non-admins upload only to their OWN department, so the field
                    is locked to userDept (read-only). Admins keep the full
                    type-ahead over every department. */}
                <div>
                    <label style={LABEL_STYLE}>
                        Domain (Department) <span style={{ color: '#f43f5e' }} aria-hidden="true">*</span>
                    </label>
                    {!isAdmin ? (
                        <div style={{ position: 'relative' }}>
                            <input
                                type="text"
                                value={value.domain || userDept || ''}
                                readOnly
                                disabled
                                aria-required="true"
                                title="Restricted to your department"
                                style={{
                                    ...INPUT_STYLE,
                                    background: '#f9fafb',
                                    color: (value.domain || userDept) ? '#374151' : '#9ca3af',
                                    cursor: 'not-allowed',
                                }}
                            />
                            <div style={{ marginTop: 4, fontSize: 10, color: '#9ca3af' }}>
                                Restricted to your department
                            </div>
                        </div>
                    ) : (
                    <div ref={deptRef} style={{ position: 'relative' }}>
                        <input
                            type="text"
                            value={deptInput}
                            onChange={e => {
                                // Pure filter — never commit/clear while typing.
                                setDeptTyping(true);
                                setDeptInput(e.target.value);
                                setDeptOpen(true);
                            }}
                            onFocus={() => {
                                setDeptTyping(true);
                                setDeptInput('');
                                setDeptOpen(true);
                            }}
                            onClick={() => {
                                setDeptTyping(true);
                                setDeptOpen(true);
                            }}
                            onBlur={() => setDeptTyping(false)}
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
                            {filteredDepts.length === 0 && (
                                <div className="kb-scope-menu__empty">
                                    No departments found
                                </div>
                            )}
                            {filteredDepts.map(dept => (
                                <div
                                    key={dept}
                                    role="option"
                                    aria-selected={value.domain === dept}
                                    className={
                                        'kb-scope-menu__item'
                                        + (value.domain === dept ? ' kb-scope-menu__item--active' : '')
                                    }
                                    // onMouseDown so the pick lands before input onBlur.
                                    onMouseDown={(e) => {
                                        e.preventDefault();
                                        setDeptTyping(false);
                                        setDeptInput(dept);
                                        onDomainChange(dept);
                                        setDeptOpen(false);
                                    }}
                                >
                                    {dept}
                                </div>
                            ))}
                        </PortalMenu>
                    </div>
                    )}
                </div>

                {/* Product — MANDATORY */}
                <div>
                    <label style={LABEL_STYLE}>
                        Product <span style={{ color: '#f43f5e' }} aria-hidden="true">*</span>
                    </label>
                    <ScopeSelect
                        value={value.product_id || ''}
                        onChange={(v) => onProductChange(v)}
                        disabled={hasNoProducts}
                        placeholder={hasNoProducts ? 'No products mapped to your department' : 'Select product'}
                        emptyText="No products mapped to your department"
                        options={products.map(p => ({ value: p.id, label: p.name }))}
                    />
                </div>

                {/* Spec Version — MANDATORY, free text (uploading a NEW version). */}
                <div>
                    <label style={LABEL_STYLE}>
                        Spec Version <span style={{ color: '#f43f5e' }} aria-hidden="true">*</span>
                    </label>
                    <input
                        type="text"
                        value={value.spec_version || ''}
                        onChange={e => onVersionChange(e.target.value)}
                        placeholder="e.g. v3, 2025.1"
                        style={INPUT_STYLE}
                    />
                </div>

                {/* Version Date — optional */}
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

                {/* Source Type — optional */}
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
            </div>

            {/* Deprecate-prior — requires Product + Domain */}
            {value.product_id && value.domain && (
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
