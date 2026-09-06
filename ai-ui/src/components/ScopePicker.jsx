// SPDX-License-Identifier: MIT
// ScopePicker — shared KB scope selector.
//
// Phase 1 wiring (kn_rewrite.md). One source of truth for the four scope
// fields users pick across both:
//   - KnowledgeBase upload (which product/version a new doc belongs to)
//   - Chat (which product/version a chat-time question should be scoped to)
//
// Stateless w.r.t. selection: parent owns `value` + `onChange`.
//
// Props:
//   value:           { product_id, domain, spec_version, version_date?,
//                      deprecate_prior?, kb_doc_id? }
//   onChange:        (next) => void   — receives the full merged value object
//   includeDocPicker: boolean         — render the doc-level picker (chat side)
//   includeUploadFields: boolean      — render version_date + deprecate_prior
//                                       (upload side)
//   disabled:        boolean          — disable all inputs (e.g. while uploading)
//   layout:          "grid" (default) | "row" — UI density variant
//
// Reuses `/products`, `/products/departments`, `/kb?product_id=...` endpoints
// already exposed by the gateway. The UI displays departments while preserving
// the existing internal `domain` field contract.

import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { API_BASE, authFetch } from "../config";

// Shared dropdown chrome — matches the indigo/violet accents used across the
// Knowledge Base surface (rounded-md, subtle ring on focus, custom chevron
// overlay). Centralised here so Domain (Department) / Product / Source Type stay visually
// in lock-step.
// `appearance-none` + the vendor-specific variants below kill the native
// dropdown arrow in Chrome/Safari/Firefox/IE so only the custom
// <SelectChevron/> overlay remains. Without [&::-ms-expand]:hidden and the
// inline `-webkit-appearance:none` style fallback some browsers still paint
// their own caret, producing the "two arrows" bug.
//
// NOTE: `text-gray-*` is intentionally omitted from the base class — the
// callsite chooses between `text-gray-700` (resolved value) and
// `text-gray-300` (placeholder) so the latter actually wins. Including
// the base color here makes the conditional class lose the Tailwind
// stylesheet-order tiebreak (lighter shades emit first, so the heavier
// gray-700 from the base class would always paint on top).
const SELECT_CLASS =
  "appearance-none [-webkit-appearance:none] [-moz-appearance:none] " +
  "[&::-ms-expand]:hidden " +
  "w-full bg-white border border-gray-200 rounded-md " +
  "px-3 py-2 pr-8 text-xs shadow-sm " +
  "hover:border-indigo-300 " +
  "focus:outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 " +
  "disabled:bg-gray-50 disabled:text-gray-400 " +
  "transition cursor-pointer";

// Inline fallback for engines that strip Tailwind's bracket variants — kept
// alongside the class so the rule wins regardless of CSS resolution order.
const SELECT_STYLE = {
  WebkitAppearance: "none",
  MozAppearance: "none",
  appearance: "none",
  backgroundImage: "none",
};

// `text-gray-*` intentionally omitted — see SELECT_CLASS note. The Spec
// Version input below appends `text-gray-700` itself so the `placeholder:`
// variant doesn't lose the order tiebreak.
const INPUT_CLASS =
  "w-full bg-white border border-gray-200 rounded-md " +
  "px-3 py-2 text-xs shadow-sm " +
  "hover:border-indigo-300 " +
  "focus:outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 " +
  "disabled:bg-gray-50 disabled:text-gray-400 " +
  "transition placeholder:text-gray-300";

function SelectChevron() {
  return (
    <ChevronDown
      size={14}
      className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400"
    />
  );
}

// ── Part U13 (2026-06-08) — docx §8 source_type enum ──────────────────────
// Mirrors the DB CHECK on knowledge_docs.source_type + document_embeddings
// .source_type. Order = upload-frequency expectation in AiNxt domain. "Other"
// is the safe default — server normalises empty string → NULL.
const SOURCE_TYPES = [
  { value: "BRD",            label: "BRD — Business Requirements" },
  { value: "FSD",            label: "FSD — Functional Spec" },
  { value: "TPMC_DECISION",  label: "TPMC Decision" },
  { value: "RBI_CIRCULAR",   label: "RBI Circular" },
  { value: "ARCHITECTURE",   label: "Architecture / Design" },
  { value: "SPEC",           label: "Spec" },
  { value: "OTHER",          label: "Other" },
];

export default function ScopePicker({
  value              = {},
  onChange,
  includeDocPicker   = false,
  includeUploadFields = false,
  disabled           = false,
  layout             = "grid",
  className          = "",
}) {
  const [products,       setProducts]       = useState([]);
  const [productsLoaded, setProductsLoaded] = useState(false);
  const [docs,           setDocs]           = useState([]);
  const [departments,    setDepartments]    = useState([]);
  const [deptsLoaded,    setDeptsLoaded]    = useState(false);
  const [deptOpen,       setDeptOpen]       = useState(false);
  const [deptInput,      setDeptInput]      = useState(value.domain || "");
  const deptRef          = useRef(null);

  // Sync input when value.domain changes from outside (e.g. clear).
  useEffect(() => { setDeptInput(value.domain || ""); }, [value.domain]);

  useEffect(() => {
    function handle(e) { if (deptRef.current && !deptRef.current.contains(e.target)) setDeptOpen(false); }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, []);

  // Load department list (same endpoint as ProductManager "New Product" form).
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await authFetch(`${API_BASE}/products/departments`);
        if (!res.ok) return;
        const data = await res.json();
        if (!alive) return;
        setDepartments((data.departments || []).filter(d => d && d.trim() !== ""));
        setDeptsLoaded(true);
      } catch { /* non-fatal */ }
    })();
    return () => { alive = false; };
  }, []);

  // Load product list once (matches the original KnowledgeBase pattern).
  //
  // Filter to ACTIVE products only. The /products endpoint surfaces the
  // caller's own PENDING_APPROVAL / REJECTED submissions alongside ACTIVE
  // ones (see routers/products_router.py L268–276) so the unfiltered list
  // mixes lifecycle states. For the upload-side ScopePicker we only want
  // products an uploaded doc can legitimately be scoped against — i.e.
  // ACTIVE. Default to ACTIVE when `status` is missing so older API
  // shapes (or admin-only endpoints that strip the field) still pass.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await authFetch(`${API_BASE}/products?limit=200`);
        if (!res.ok) return;
        const data = await res.json();
        if (!alive) return;
        setProducts(
          (data.products || data.items || [])
            .filter(p => (p?.status || "ACTIVE") === "ACTIVE")
        );
        setProductsLoaded(true);
      } catch {/* non-fatal */}
    })();
    return () => { alive = false; };
  }, []);

  // Doc picker reloads whenever scope narrows (product / version change).
  // Clears the dropdown synchronously on the way in so the user never sees
  // the previous product's docs briefly while the new fetch is in flight,
  // and uses AbortController so rapid edits cancel orphan requests instead
  // of letting them pile up on the backend. (Audit Fix #5)
  useEffect(() => {
    if (!includeDocPicker || !value.product_id) {
      setDocs([]);
      return;
    }
    setDocs([]); // prevent a flash of stale cross-product docs
    const controller = new AbortController();
    (async () => {
      try {
        const qs = new URLSearchParams({
          product_id: value.product_id,
          status: "APPROVED",
          limit: "200",
        });
        if (value.spec_version) qs.set("spec_version", value.spec_version);
        if (value.domain)       qs.set("domain",       value.domain);
        const res = await authFetch(`${API_BASE}/kb?${qs.toString()}`, {
          signal: controller.signal,
        });
        if (!res.ok) return;
        const data = await res.json();
        if (controller.signal.aborted) return;
        setDocs(data.docs || data.items || []);
      } catch (e) {
        // AbortError is expected on rapid edits — swallow silently.
        if (e?.name !== "AbortError") {/* non-fatal */}
      }
    })();
    return () => controller.abort();
  }, [includeDocPicker, value.product_id, value.spec_version, value.domain]);

  const merge = (patch) => onChange?.({ ...value, ...patch });

  // Changing product or version invalidates the doc selection.
  const onProductChange = (pid) => merge({ product_id: pid || null, kb_doc_id: null });
  const onDomainChange  = (d)   => merge({ domain:     d   || null });
  const onVersionChange = (v)   => merge({ spec_version: v || null, kb_doc_id: null });
  const onDocChange     = (did) => merge({ kb_doc_id:  did || null });

  const gridClass = layout === "row"
    ? "flex flex-wrap gap-2"
    : "grid grid-cols-2 gap-3";
  const hasNoProducts = productsLoaded && products.length === 0;

  return (
    <div className={`space-y-3 ${disabled ? "opacity-50 pointer-events-none" : ""} ${className}`}>
      <div className={gridClass}>
        {/* Domain (Department) — MANDATORY (dynamic list from /products/departments) */}
        <div className={layout === "row" ? "flex-1 min-w-[120px]" : ""}>
          <label className="block text-xs font-medium text-gray-700 mb-1.5">
            Domain (Department) <span className="text-rose-500" aria-hidden="true">*</span>
          </label>
          <div ref={deptRef} className="relative">
            <input
              type="text"
              value={deptInput}
              onChange={e => {
                setDeptInput(e.target.value);
                setDeptOpen(true);
                // If the user types an exact match, commit it immediately.
                const match = departments.find(d => d.toLowerCase() === e.target.value.toLowerCase());
                if (match) onDomainChange(match);
                else if (value.domain) onDomainChange("");
              }}
              onFocus={() => setDeptOpen(true)}
              onClick={() => setDeptOpen(true)}
              placeholder="Select domain"
              aria-required="true"
              autoComplete="off"
              className={`${SELECT_CLASS} ${!value.domain ? "text-gray-300 placeholder:text-gray-300" : "text-gray-700"}`}
              style={SELECT_STYLE}
            />
            <div className="absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none">
              <SelectChevron />
            </div>
            {deptOpen && (
              <div className="absolute z-50 mt-1 w-full bg-white border border-gray-200 rounded-md shadow-lg max-h-52 overflow-y-auto">
                {departments.filter(d => d.toLowerCase().includes(deptInput.toLowerCase())).length === 0 && (
                  <div className="px-3 py-2 text-xs text-gray-400">No departments found</div>
                )}
                {departments.filter(d => d.toLowerCase().includes(deptInput.toLowerCase())).map(dept => (
                  <div
                    key={dept}
                    onClick={() => { onDomainChange(dept); setDeptOpen(false); }}
                    className={`px-3 py-2 text-sm cursor-pointer ${
                      value.domain === dept ? "bg-indigo-50 text-indigo-700 font-medium" : "hover:bg-gray-50"
                    }`}
                  >
                    {dept}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Product — MANDATORY */}
        <div className={layout === "row" ? "flex-1 min-w-[140px]" : ""}>
          <label className="block text-xs font-medium text-gray-700 mb-1.5">
            Product <span className="text-rose-500" aria-hidden="true">*</span>
          </label>
          <div className="relative">
            <select
              value={value.product_id || ""}
              onChange={e => onProductChange(e.target.value)}
              aria-required="true"
              disabled={hasNoProducts}
              className={`${SELECT_CLASS} ${!value.product_id ? "text-gray-300" : "text-gray-700"}`}
              style={SELECT_STYLE}
            >
              {hasNoProducts ? (
                <option value="" disabled>No products mapped to your department</option>
              ) : (
                <option value="" hidden disabled>Select product</option>
              )}
              {products.map(p => (
                <option key={p.id} value={p.id} className="text-gray-700">{p.name}</option>
              ))}
            </select>
            <SelectChevron />
          </div>
        </div>

        {/* Spec Version — MANDATORY */}
        <div className={layout === "row" ? "flex-1 min-w-[100px]" : ""}>
          <label className="block text-xs font-medium text-gray-700 mb-1.5">
            Spec Version <span className="text-rose-500" aria-hidden="true">*</span>
          </label>
          <input
            type="text"
            value={value.spec_version || ""}
            onChange={e => onVersionChange(e.target.value)}
            placeholder="e.g. v3, 2025.1"
            className={`${INPUT_CLASS} text-gray-700`}
          />
        </div>

        {/* Version Date — upload only, optional */}
        {includeUploadFields && (
          <div className={layout === "row" ? "flex-1 min-w-[120px]" : ""}>
            <label className="block text-xs font-medium text-gray-700 mb-1.5">
              Version Date <span className="text-gray-400 font-normal">(optional)</span>
            </label>
            <input
              type="date"
              value={value.version_date || ""}
              onChange={e => merge({ version_date: e.target.value || null })}
              /* `<input type="date">` doesn't render the HTML `placeholder`
                  attribute — what looks like "dd-mm-yyyy" is the browser's
                  UA-styled datetime-edit hint, painted by the
                  ::-webkit-datetime-edit pseudo-elements. We light it up
                  via Tailwind arbitrary variants when no value is picked,
                  and fall back to colouring the whole input gray-300 for
                  Firefox (which ignores the WebKit pseudos). Once the
                  user picks a date the regular gray-700 takes over. */
              className={`${INPUT_CLASS} ${
                !value.version_date
                  ? "text-gray-300 [&::-webkit-datetime-edit]:text-gray-300 [&::-webkit-datetime-edit-fields-wrapper]:text-gray-300 [&::-webkit-datetime-edit-text]:text-gray-300 [&::-webkit-datetime-edit-month-field]:text-gray-300 [&::-webkit-datetime-edit-day-field]:text-gray-300 [&::-webkit-datetime-edit-year-field]:text-gray-300"
                  : "text-gray-700"
              }`}
            />
          </div>
        )}

        {/* Source Type — upload only (Part U13 / docx §8). Captures doc kind
            so retrieval can filter by type and the citation footer can show
            a typed badge. Empty selection → server stores NULL (legacy). */}
        {includeUploadFields && (
          <div className={layout === "row" ? "flex-1 min-w-[160px]" : ""}>
            <label className="block text-xs font-medium text-gray-700 mb-1.5">
              Source Type <span className="text-gray-400 font-normal">(optional)</span>
            </label>
            <div className="relative">
              <select
                value={value.source_type || ""}
                onChange={e => merge({ source_type: e.target.value || null })}
                disabled={disabled}
                className={`${SELECT_CLASS} ${!value.source_type ? "text-gray-300" : "text-gray-700"}`}
                style={SELECT_STYLE}
              >
                <option value="" hidden disabled>Select source type</option>
                {SOURCE_TYPES.map(t => (
                  <option key={t.value} value={t.value} className="text-gray-700">{t.label}</option>
                ))}
              </select>
              <SelectChevron />
            </div>
          </div>
        )}
      </div>

      {/* Document picker — chat side. Narrows further to a single specific doc. */}
      {includeDocPicker && value.product_id && (
        <div>
          <label className="block text-xs font-medium text-gray-700 mb-1.5">Document (optional)</label>
          <div className="relative">
            <select
              value={value.kb_doc_id || ""}
              onChange={e => onDocChange(e.target.value)}
              className={`${SELECT_CLASS} ${!value.kb_doc_id ? "text-gray-300" : "text-gray-700"}`}
              style={SELECT_STYLE}
            >
              <option value="">— Any document in this scope —</option>
              {docs.map(d => (
                <option key={d.id} value={d.id}>
                  {(d.display_name || d.title || d.namespace || d.id)}
                  {d.spec_version ? ` · ${d.spec_version}` : ""}
                </option>
              ))}
            </select>
            <SelectChevron />
          </div>
          <p className="mt-1 text-[10px] text-gray-400">
            Pin one specific document. When set, the Coverage tier reads every section of just this doc.
          </p>
        </div>
      )}

      {/* Deprecate-prior — upload only, requires product + domain */}
      {includeUploadFields && value.product_id && value.domain && (
        <label className="flex items-center gap-2 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={!!value.deprecate_prior}
            onChange={e => merge({ deprecate_prior: e.target.checked })}
            className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
          />
          <span className="text-xs text-gray-600">
            Deprecate prior versions of this product + domain on approval
          </span>
        </label>
      )}
    </div>
  );
}
