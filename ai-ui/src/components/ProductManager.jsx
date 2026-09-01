// SPDX-License-Identifier: Apache-2.0
// ============================================================
// ProductManager — Product Ontology UI
// Dept-scoped: creation requires can_approve (threshold set by APPROVAL_AD_LEVEL)
// Departments mapped via dept_product_mappings table
// ============================================================

import { useState, useEffect, useRef } from "react";
import { X, ChevronDown, Pencil, Trash2, Plus } from "lucide-react";
import { API_BASE as API, authFetch, apiFetch } from "../config";
import { decryptPii } from "../utils/piiCrypto";

// PII payload encryption flag (core/pii_crypto.py) — module-level singleton
// promise, fetched once from the unauthenticated /auth/ui-config endpoint.
let _pmPiiEnabledPromise = null;
function pmPiiEnabled() {
  if (!_pmPiiEnabledPromise) {
    _pmPiiEnabledPromise = apiFetch(`${API}/auth/ui-config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => !!d?.pii_payload_encryption_enabled)
      .catch(() => false);
  }
  return _pmPiiEnabledPromise;
}
import { useToast, useConfirm } from './ui/DialogProvider.jsx';
import { toIST, toISTDate } from "../utils/time";
import {
  validateProductName,
  validateProductCode,
  validateDescription,
  validateURL,
  validateRepoName,
  getErrorMessage,
} from "../utils/securityValidation";

// ── Searchable multi-select dropdown ──────────────────────────
function MultiSelectDept({ options, selected, onChange, hasErrors }) {
  const [open, setOpen]       = useState(false);
  const [search, setSearch]   = useState("");
  const ref                   = useRef(null);

  useEffect(() => {
    function handle(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false); }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, []);

  const filtered = options.filter(o =>
    o.toLowerCase().includes(search.toLowerCase())
  );

  function toggle(dept) {
    onChange(
      selected.includes(dept)
        ? selected.filter(d => d !== dept)
        : [...selected, dept]
    );
  }

  function remove(dept, e) {
    e.stopPropagation();
    onChange(selected.filter(d => d !== dept));
  }

  return (
    <div ref={ref} className="relative">
      <div
        onClick={() => setOpen(o => !o)}
        className={`min-h-[38px] w-full bg-white rounded px-2 py-1.5 flex flex-wrap gap-1 cursor-pointer focus-within:ring-1 focus-within:ring-blue-500 ${
          hasErrors 
            ? "border border-red-500 ring-1 ring-red-200" 
            : "border border-gray-300"
        }`}
      >
        {selected.length === 0 && (
          <span className="text-gray-400 text-sm self-center">Select departments…</span>
        )}
        {selected.map(d => (
          <span key={d} className="flex items-center gap-1 brand-grad text-white hover:opacity-70 text-xs px-2 py-0.5 rounded-full">
            {d}
            <button type="button" onClick={e => remove(d, e)} className="cursor-pointer">
              <X size={10} />
            </button>
          </span>
        ))}
        <ChevronDown size={14} className={`ml-auto self-center text-gray-400 flex-shrink-0 transition-transform ${open ? "rotate-180" : ""}`} />
      </div>

      {open && (
        <div className="absolute z-50 mt-1 w-full bg-white border border-gray-200 rounded-md shadow-lg">
          <div className="p-2 border-b border-gray-100">
            <input
              autoFocus
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search departments…"
              className="w-full text-sm border border-gray-200 rounded px-2 py-1 outline-none focus:border-indigo-600"
            />
          </div>
          <div className="max-h-52 overflow-y-auto">
            {filtered.length === 0 && (
              <div className="px-3 py-2 text-xs text-gray-400">No departments found</div>
            )}
            {filtered.map(dept => (
              <label
                key={dept}
                className="flex items-center gap-2 px-3 py-2 text-sm hover:bg-gray-50 cursor-pointer"
              >
                <input
                  type="checkbox"
                  checked={selected.includes(dept)}
                  onChange={() => toggle(dept)}
                  className="accent-indigo-700"
                />
                {dept}
              </label>
            ))}
          </div>
          {selected.length > 0 && (
            <div className="px-3 py-1.5 border-t border-gray-100 text-xs text-gray-400">
              {selected.length} selected
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── URL parsers ────────────────────────────────────────────────
function parseJiraKey(url) {
  if (!url) return "";
  const s = url.trim();
  // Already a raw project key (e.g. "RUPAY" or "AiNxt")
  if (/^[A-Z][A-Z0-9_]{0,19}$/i.test(s)) return s.toUpperCase();
  // /projects/KEY (Jira Cloud software/core/next-gen projects)
  let m = s.match(/\/projects\/([A-Z][A-Z0-9_]+)/i);
  if (m) return m[1].toUpperCase();
  // /browse/KEY or /browse/KEY-123
  m = s.match(/\/browse\/([A-Z][A-Z0-9_]+)(?:-\d+)?/i);
  if (m) return m[1].toUpperCase();
  // /jira/KEY or /jira/software/KEY (some on-prem formats)
  m = s.match(/\/jira\/(?:software\/)?([A-Z][A-Z0-9_]+)/i);
  if (m) return m[1].toUpperCase();
  // Last-ditch: extract last all-caps word from URL path (e.g. /board/RUPAY/sprint)
  const pathParts = (s.split("?")[0].split("/")).filter(Boolean);
  for (let i = pathParts.length - 1; i >= 0; i--) {
    if (/^[A-Z][A-Z0-9_]{1,19}$/i.test(pathParts[i]) && !/^(jira|software|core|board|boards|backlog|sprint|issues|settings|projects|wiki|spaces|pages|overview|rest|api)$/i.test(pathParts[i])) {
      return pathParts[i].toUpperCase();
    }
  }
  return "";
}

function parseConfluenceSpace(url) {
  if (!url) return "";
  const s = url.trim();
  // Already a raw space key (e.g. "RUPAY")
  if (/^~?[A-Z][A-Z0-9_]{0,19}$/i.test(s)) return s.toUpperCase();
  // /spaces/KEY (Confluence Cloud)
  const m = s.match(/\/spaces\/(~?[A-Z][A-Z0-9_]+)/i);
  if (m) return m[1].toUpperCase();
  return "";
}

// ── People with Access ─────────────────────────────────────────
function PeopleWithAccess({ people }) {
  const [filter, setFilter] = useState("");

  const filtered = filter.trim()
    ? people.filter(p =>
        p.name.toLowerCase().includes(filter.toLowerCase()) ||
        p.email.toLowerCase().includes(filter.toLowerCase()) ||
        (p.title || "").toLowerCase().includes(filter.toLowerCase()) ||
        p.department.toLowerCase().includes(filter.toLowerCase())
      )
    : people;

  const depts = [...new Set(filtered.map(p => p.department))];

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-semibold text-gray-800">People with Access</h3>
        <span className="text-xs text-gray-400">AD org tree · {people.length} people</span>
      </div>

      {/* Filter input */}
      <input
        value={filter}
        onChange={e => setFilter(e.target.value)}
        placeholder="Filter by name, email, title or department…"
        className="w-full mb-3 bg-gray-50 border border-gray-200 rounded px-3 py-1.5 text-sm text-gray-900 focus:outline-none focus:border-indigo-300"
      />

      {people.length === 0 ? (
        <p className="text-sm text-gray-400">No org tree data for mapped departments.</p>
      ) : filtered.length === 0 ? (
        <p className="text-sm text-gray-400">No matches for "{filter}".</p>
      ) : (
        /* Fixed height + scrollbar */
        <div className="h-72 overflow-y-auto rounded border border-gray-100 divide-y divide-gray-100">
          {depts.map(dept => (
            <div key={dept}>
              <div className="sticky top-0 bg-gray-50 px-3 py-1.5 text-xs font-semibold text-gray-500 uppercase tracking-wide border-b border-gray-100">
                {dept}
              </div>
              {filtered.filter(p => p.department === dept).map(p => (
                <div key={p.email} className="flex items-center justify-between px-3 py-2 hover:bg-gray-50 text-sm">
                  <div className="min-w-0">
                    <span className="text-gray-800 font-medium">{p.name}</span>
                    <span className="text-gray-400 text-xs ml-2 truncate">{p.email}</span>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0 ml-2">
                    <span className="text-xs text-gray-400 hidden sm:block truncate max-w-[140px]">{p.title}</span>
                    {p.can_approve && (
                      <span className="text-xs px-1.5 py-0.5 bg-amber-50 border border-amber-200 text-amber-700 rounded-full whitespace-nowrap">approver</span>
                    )}
                    <span className="text-xs text-gray-300 w-6 text-right">L{p.level}</span>
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      <p className="text-xs text-gray-400 mt-2">
        Access is automatic for all users in mapped departments. Synced nightly from Active Directory.
      </p>
    </div>
  );
}

export default function ProductManager({ user }) {
  const { toast }   = useToast();
  const { confirm } = useConfirm();
  const [products, setProducts]     = useState([]);
  const [pending, setPending]       = useState([]);
  const [loading, setLoading]       = useState(true);
  const [selected, setSelected]     = useState(null);
  const [creating, setCreating]     = useState(false);
  const [tab, setTab]               = useState("active"); // "active" | "pending"
  const [productSearch, setProductSearch] = useState("");
  const [error, setError]           = useState("");
  const [success, setSuccess]       = useState("");

  const [allDepts, setAllDepts]     = useState([]);

  const [form, setForm] = useState({
    name: "", code: "", description: "", jira_url: "", confluence_url: "", departments: [], repos: []
  });
  const [formErrors, setFormErrors] = useState({
    name: "", code: "", description: "", jira_url: "", confluence_url: "", departments: "", repoInput: ""
  });
  const [repoCreateInput, setRepoCreateInput] = useState("");
  const [saving, setSaving] = useState(false);

  // Inline edit state for Jira / Confluence URLs
  const [editing, setEditing]       = useState(false);
  const [editForm, setEditForm]     = useState({ jira_url: "", confluence_url: "" });
  const [editFormErrors, setEditFormErrors] = useState({ jira_url: "", confluence_url: "" });
  const [editSaving, setEditSaving] = useState(false);

  // Add repo
  const [repoInput, setRepoInput]   = useState("");
  const [repoInputError, setRepoInputError] = useState("");

  // Field validation helper
  function validateField(fieldName, value) {
    // Mandatory checks first — custom error, not from validator
    if (fieldName === "name" && (!value || !value.trim())) return "Product name is required";
    if (fieldName === "code" && (!value || !value.trim())) return "Product code is required";

    let result;
    switch (fieldName) {
      case "name":
        result = validateProductName(value);
        break;
      case "code":
        result = validateProductCode(value);
        break;
      case "description":
        result = validateDescription(value);
        break;
      case "jira_url":
        result = validateURL(value, { fieldName: "Jira URL" });
        break;
      case "confluence_url":
        result = validateURL(value, { fieldName: "Confluence URL" });
        break;
      case "repoInput":
        result = validateRepoName(value);
        break;
      default:
        return "";
    }
    return result.isValid ? "" : result.errors[0]?.message || "";
  }

  function handleBlur(fieldName) {
    const error = validateField(fieldName, form[fieldName]);
    setFormErrors(prev => ({ ...prev, [fieldName]: error }));
  }

  function handleChange(fieldName, value) {
    setForm(prev => ({ ...prev, [fieldName]: value }));
    // Clear error when user starts typing
    if (formErrors[fieldName]) {
      setFormErrors(prev => ({ ...prev, [fieldName]: "" }));
    }
  }

  function handleRepoInputChange(value) {
    setRepoCreateInput(value);
    if (formErrors.repoInput) {
      setFormErrors(prev => ({ ...prev, repoInput: "" }));
    }
  }

  function handleRepoInputBlur() {
    if (repoCreateInput.trim()) {
      const error = validateField("repoInput", repoCreateInput);
      setFormErrors(prev => ({ ...prev, repoInput: error }));
    }
  }

  const isAdmin      = user?.role === "admin";
  const adLevel      = user?.ad_level ?? 6;
  const canApprove   = user?.can_approve === true;
  const canSeePending = isAdmin || canApprove;  // admins always see pending tab
  const canCreate    = true;   // all users can submit; non-admin → PENDING_APPROVAL (4-eyes)
  const currentEmail = user?.email || "";

  useEffect(() => {
    loadProducts();
    if (canSeePending) loadPending();  // load pending on mount so badge count is immediate
    authFetch(`${API}/products/departments`)
      .then(r => r.json())
      .then(d => setAllDepts(d.departments || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (canSeePending && tab === "pending") loadPending();
  }, [tab]);

  function loadProducts() {
    setLoading(true);
    authFetch(`${API}/products`)
      .then(r => r.json())
      .then(d => setProducts(d.products || []))
      .catch(() => setError("Failed to load products"))
      .finally(() => setLoading(false));
  }

  function loadPending() {
    authFetch(`${API}/products/pending`)
      .then(r => r.json())
      .then(d => setPending(d.products || []))
      .catch(() => {});
  }

  function loadDetail(productId) {
    authFetch(`${API}/products/${productId}`)
      .then(r => r.json())
      .then(async p => {
        // Decrypt people[].name/.email immediately after fetch (before
        // setSelected) so PeopleWithAccess's client-side substring search
        // keeps working — matching against ciphertext would be meaningless.
        const piiOn = await pmPiiEnabled();
        p.people = await Promise.all((p.people || []).map(async person => ({
          ...person,
          name:  await decryptPii(person.name,  piiOn),
          email: await decryptPii(person.email, piiOn),
        })));
        setSelected(p); setEditing(false);
      })
      .catch(() => setError("Failed to load product details"));
  }

  async function createProduct(e) {
    e.preventDefault();

    // Validate all required fields
    const errors = {
      name: validateField("name", form.name),
      code: validateField("code", form.code),
      description: validateField("description", form.description),
      jira_url: validateField("jira_url", form.jira_url),
      confluence_url: validateField("confluence_url", form.confluence_url),
      repoInput: "",
    };

    if (form.departments.length === 0) {
      errors.departments = "Select at least one department";
    }

    // Check if any errors exist
    const hasErrors = Object.values(errors).some(e => e !== "");
    if (hasErrors) {
      setFormErrors(errors);
      setError("Please fix the validation errors before submitting");
      return;
    }

    const jira_project_key  = parseJiraKey(form.jira_url);
    const confluence_space  = parseConfluenceSpace(form.confluence_url);
    setSaving(true);
    setError("");
    try {
      const res = await authFetch(`${API}/products`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name:            form.name,
          code:            form.code,
          description:     form.description,
          jira_project_key,
          confluence_space,
          jira_url:        form.jira_url,
          confluence_url:  form.confluence_url,
          departments:     form.departments,
          repos:           form.repos,
        }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Create failed");
      }
      const created = await res.json();
      if (created.status === "PENDING_APPROVAL") {
        setSuccess("Product submitted for approval — a senior (level ≤ 3) will review it.");
      } else {
        setSuccess("Product created successfully.");
      }
      setCreating(false);
      setRepoCreateInput("");
      setForm({ name: "", code: "", description: "", jira_url: "", confluence_url: "", departments: [], repos: [] });
      loadProducts();
      if (canSeePending) {
        loadPending();
        if (created.status === "PENDING_APPROVAL") setTab("pending");
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  function handleEditBlur(fieldName) {
    const error = validateField(fieldName, editForm[fieldName]);
    setEditFormErrors(prev => ({ ...prev, [fieldName]: error }));
  }

  function handleEditChange(fieldName, value) {
    setEditForm(prev => ({ ...prev, [fieldName]: value }));
    if (editFormErrors[fieldName]) {
      setEditFormErrors(prev => ({ ...prev, [fieldName]: "" }));
    }
  }

  async function saveEdit() {
    if (!selected) return;

    // Validate URLs before saving
    const jiraError = validateField("jira_url", editForm.jira_url);
    const confError = validateField("confluence_url", editForm.confluence_url);

    if (jiraError || confError) {
      setEditFormErrors({ jira_url: jiraError, confluence_url: confError });
      setError("Please fix the validation errors before saving");
      return;
    }

    setEditSaving(true);
    setError("");
    try {
      const jira_project_key  = parseJiraKey(editForm.jira_url);
      const confluence_space  = parseConfluenceSpace(editForm.confluence_url);
      const res = await authFetch(`${API}/products/${selected.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jira_url:        editForm.jira_url   || null,
          confluence_url:  editForm.confluence_url || null,
          jira_project_key: jira_project_key  || null,
          confluence_space: confluence_space  || null,
        }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Update failed");
      }
      setSuccess("Product updated.");
      loadDetail(selected.id);
    } catch (err) {
      setError(err.message);
    } finally {
      setEditSaving(false);
    }
  }

  async function deleteProduct() {
    if (!selected) return;
    const ok = await confirm({ title: "Delete Product", message: `Delete "${selected.name}"? This cannot be undone.`, confirmLabel: "Delete" });
    if (!ok) return;
    try {
      const res = await authFetch(`${API}/products/${selected.id}`, { method: "DELETE" });
      if (!res.ok && res.status !== 204) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Delete failed");
      }
      setSuccess("Product deleted.");
      setSelected(null);
      loadProducts();
    } catch (err) {
      setError(err.message);
    }
  }

  async function approveProduct(productId) {
    try {
      const res = await authFetch(`${API}/products/${productId}/approve`, { method: "POST" });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Approve failed");
      }
      setSuccess("Product approved.");
      loadPending();
      loadProducts();
    } catch (err) {
      setError(err.message);
    }
  }

  async function rejectProduct(productId) {
    const note = prompt("Rejection reason (optional):");
    try {
      const url = note
        ? `${API}/products/${productId}/reject?note=${encodeURIComponent(note)}`
        : `${API}/products/${productId}/reject`;
      const res = await authFetch(url, { method: "POST" });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Reject failed");
      }
      setSuccess("Product rejected.");
      loadPending();
    } catch (err) {
      setError(err.message);
    }
  }

  async function addRepo() {
    if (!repoInput.trim() || !selected) return;

    // Validate repo name
    const validation = validateRepoName(repoInput.trim());
    if (!validation.isValid) {
      setRepoInputError(validation.errors[0]?.message || "Invalid repository name");
      return;
    }

    try {
      const res = await authFetch(`${API}/products/${selected.id}/repos`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_name: repoInput.trim() }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Add repo failed");
      }
      setRepoInput("");
      setRepoInputError("");
      setSuccess("Repo added");
      loadDetail(selected.id);
    } catch (err) {
      setError(err.message);
    }
  }

  async function removeRepo(repoName) {
    if (!selected) return;
    try {
      await authFetch(`${API}/products/${selected.id}/repos/${encodeURIComponent(repoName)}`, { method: "DELETE" });
      loadDetail(selected.id);
    } catch {
      setError("Remove repo failed");
    }
  }

  // ────────────────────────────────────────────────────────────

  return (
    <div className="flex h-full overflow-hidden bg-gray-50 text-gray-900">

      {/* ── Product list sidebar ─────────────────────────── */}
      <div className="w-72 bg-gray-50 border-r border-gray-200 flex flex-col flex-shrink-0">
        <div className="p-4 border-b border-gray-200 flex items-center justify-between">
          <h2 className="text-sm font-semibold  text-indigo-700">Products</h2>
          {canCreate && (
            <button
              onClick={() => { setCreating(true); setSelected(null); setEditing(false); setSuccess(""); setError(""); }}
              className="flex items-center gap-1 text-xs font-medium px-2.5 py-1 border rounded brand-grad hover:opacity-70 text-white cursor-pointer"
            >
              <Plus size={12} /> New Product
            </button>
          )}
        </div>

        {/* Tabs: Active | Request Status (all users) */}
        <div className="flex border-b border-gray-200">
          {["active", "pending"].map(t => {
            const myPendingCount = products?.filter(p => p.status === "PENDING_APPROVAL").length;
            const badgeCount = canSeePending ? pending.length : myPendingCount;
            return (
              <button
                key={t}
                onClick={() => { setTab(t); setSelected(null); setCreating(false); setSuccess(""); setError(""); }}
                className={`flex-1 py-2 text-xs font-medium transition-colors cursor-pointer ${tab === t ? "border-b-2 border-indigo-600 text-indigo-600" : "text-gray-400 hover:text-gray-600"}`}
              >
                {t === "pending"
                  ? <>Request Status {badgeCount > 0 && <span className="ml-1 bg-amber-400 text-white text-[9px] px-1.5 py-0.5 rounded-full">{badgeCount}</span>}</>
                  : "Active"}
              </button>
            );
          })}
        </div>

        <div className="px-3 py-2 border-b border-gray-100">
          <input
            value={productSearch}
            onChange={e => setProductSearch(e.target.value)}
            placeholder={tab === "pending" ? "Search requests..." : "Search products..."}
            className="w-full px-2.5 py-1.5 text-xs border border-gray-200 rounded-md outline-none focus:border-indigo-300 shadow-sm bg-white"
          />
        </div>

        <div className="flex-1 overflow-y-auto py-1">
          {tab === "active" && (
            loading ? (
              <div className="p-4 text-sm text-gray-400">Loading…</div>
            ) : products.length === 0 ? (
              <div className="p-4 text-sm text-gray-400">
                {canCreate ? "No products yet. Create one." : "No products available for your department."}
              </div>
            ) : (
              products?.filter(p => p.status === "ACTIVE" && (!productSearch || p.name.toLowerCase().includes(productSearch.toLowerCase()) || (p.code || "").toLowerCase().includes(productSearch.toLowerCase()))).map(p => (
                <div 
                className={`cursor-pointer text-left px-3 py-2.5 m-1 border-b-1 border-b-gray-100 rounded overflow-x-hidden transition-colors ${selected?.id === p.id ? "bg-indigo-50 border-l-2 border-l-indigo-500" : "hover:bg-gray-100"}`}
                role="button"
                onClick={() => { setCreating(false); loadDetail(p.id); setSuccess(""); setError(""); }}
                >
                  <div
                  key={p.id}
                  onClick={() => { setCreating(false); loadDetail(p.id); setSuccess(""); setError(""); }}
                  className={`text-left`}
                >
                  <div className="flex items-center gap-2">
                    <span className={`font-medium text-sm ${selected?.id === p.id ? "text-indigo-700 font-semibold" : "text-gray-600"}`}>{p.name}</span>
                  </div>
                  <div className="text-xs text-gray-400 mt-0.5">{p.code}</div>
                  {p.created_at && <div className="text-[10px] text-gray-400 mt-0.5">Created {toISTDate(p.created_at)}</div>}
                </div>
                </div>
                
              ))
            )
          )}

          {tab === "pending" && (() => {
            // Build the full request status list:
            // For approvers: others' dept PENDING items (from /products/pending) + own PENDING/REJECTED
            // For regular users: their own PENDING + REJECTED items from products list
            const ownRequests = products?.filter(p => p.status === "PENDING_APPROVAL" || p.status === "REJECTED");
            const approverPending = canSeePending ? pending : [];
            // Deduplicate by id — own items take precedence (they have more fields)
            const ownIds = new Set(ownRequests.map(p => p.id));
            const mergedPending = [
              ...approverPending.filter(p => !ownIds.has(p.id)).map(p => ({ ...p, status: "PENDING_APPROVAL" })),
              ...ownRequests,
            ];
            const filtered = mergedPending.filter(p =>
              !productSearch ||
              (p.name || "").toLowerCase().includes(productSearch.toLowerCase()) ||
              (p.requested_by || "").toLowerCase().includes(productSearch.toLowerCase())
            );
            if (filtered.length === 0) return (
              <div className="p-4 text-sm text-gray-400">
                {mergedPending.length === 0 ? "No requests yet." : "No matches"}
              </div>
            );
            return filtered.map(p => (
              <div key={p.id} className="px-4 py-3 border-b border-gray-100 hover:bg-gray-100 m-1 rounded">
                <div className="font-medium text-sm text-gray-600">{p.name}</div>
                <div className="text-xs text-gray-400 mt-0.5">{p.code}</div>
                {(p.requested_by || p.created_by) && (
                  <div className="text-xs text-gray-500 mt-0.5">
                    Submitted by <span className="font-medium">{p.requested_by || p.created_by}</span>
                    {p.created_at && (
                      <> · {toIST(p.created_at)}</>
                    )}
                  </div>
                )}
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {p.status === "REJECTED" ? (
                    <span className="text-[10px] px-1.5 py-0.5 bg-red-50 border border-red-200 text-red-600 rounded font-medium">
                      Rejected
                    </span>
                  ) : (
                    <span className="text-[10px] text-yellow-700 bg-yellow-50 border border-yellow-200 px-1.5 py-0.5 rounded font-medium">
                      Awaiting approval — action available in Inbox
                    </span>
                  )}
                </div>
                {p.status === "REJECTED" && (p.review_note || p.reviewed_by) && (
                  <div className="mt-1 text-xs text-red-600 bg-red-50 border border-red-100 rounded px-2 py-1">
                    {p.reviewed_by && <span className="font-medium">By {p.reviewed_by}. </span>}
                    {p.review_note && <><span className="font-medium">Reason:</span> {p.review_note}</>}
                  </div>
                )}
              </div>
            ));
          })()}
        </div>
      </div>

      {/* ── Main area ─────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto p-6 bg-white">
        {error   && <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-600 text-sm">{error}</div>}
        {success && <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded text-green-600 text-sm">{success}</div>}

        {/* Create form */}
        {creating && (
          <div className="bg-white border border-gray-200 rounded-xl p-5 mb-6 max-w-2xl shadow-sm">
            <h3 className="text-lg font-semibold mb-4 text-gray-800">Create Product</h3>
            {!isAdmin && (
              <div className="mb-4 p-3 bg-amber-50 border border-amber-200 rounded text-amber-700 text-sm">
                Your request will be submitted for approval (4-eyes rule — a different approver in your department will review it).
              </div>
            )}
            <form onSubmit={createProduct} className="space-y-3" noValidate>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Name *</label>
                  <input
                    value={form.name}
                    onChange={e => handleChange("name", e.target.value)}
                    onBlur={() => handleBlur("name")}
                    className={`w-full bg-white border rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${formErrors.name ? "border-red-500" : "border-gray-300"}`}
                    placeholder="e.g. Payments Gateway"
                  />
                  {formErrors.name && (
                    <p className="mt-1 text-xs text-red-600">{formErrors.name}</p>
                  )}
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Code * (alphanumeric)</label>
                  <input
                    value={form.code}
                    onChange={e => handleChange("code", e.target.value.toUpperCase())}
                    onBlur={() => handleBlur("code")}
                    className={`w-full bg-white border rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${formErrors.code ? "border-red-500" : "border-gray-300"}`}
                    placeholder="RUPAY"
                  />
                  {formErrors.code && (
                    <p className="mt-1 text-xs text-red-600">{formErrors.code}</p>
                  )}
                </div>
              </div>

              <div>
                <label className="block text-xs text-gray-500 mb-1">Description <span className="text-gray-400">(optional)</span></label>
                <textarea
                  value={form.description}
                  onChange={e => handleChange("description", e.target.value)}
                  onBlur={() => handleBlur("description")}
                  rows={2}
                  className={`w-full bg-white border rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${formErrors.description ? "border-red-500" : "border-gray-300"}`}
                  placeholder="Enter product description..."
                />
                {formErrors.description && (
                  <p className="mt-1 text-xs text-red-600">{formErrors.description}</p>
                )}
              </div>

              <div>
                <label className="block text-xs text-gray-500 mb-1">
                  Departments * <span className="text-gray-400">(product visible to users in these departments)</span>
                </label>
                {allDepts.length === 0 ? (
                  <p className="text-xs text-gray-400 py-1">No departments loaded — org tree may be empty.</p>
                ) : (
                  <>
                    <MultiSelectDept
                      options={allDepts}
                      selected={form.departments}
                      hasErrors={!!formErrors.departments}
                      onChange={depts => {
                        handleChange("departments", depts);
                        // Validate immediately on selection change
                        if (depts.length === 0) {
                          setFormErrors(prev => ({ ...prev, departments: "Select at least one department" }));
                        } else {
                          setFormErrors(prev => ({ ...prev, departments: "" }));
                        }
                      }}
                    />
                    {formErrors.departments && (
                      <p className="mt-1 text-xs text-red-600">{formErrors.departments}</p>
                    )}
                  </>
                )}
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Jira Project URL</label>
                  <input
                    value={form.jira_url}
                    onChange={e => handleChange("jira_url", e.target.value)}
                    onBlur={() => handleBlur("jira_url")}
                    className={`w-full bg-white border rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${formErrors.jira_url ? "border-red-500" : "border-gray-300"}`}
                    placeholder="https://ainxt.atlassian.net/jira/software/projects/RUPAY/boards"
                  />
                  {formErrors.jira_url && (
                    <p className="mt-1 text-xs text-red-600">{formErrors.jira_url}</p>
                  )}
                  {form.jira_url && !formErrors.jira_url && (
                    <p className="mt-1 text-xs text-gray-400">
                      Project key:{" "}
                      {parseJiraKey(form.jira_url)
                        ? <span className="font-mono font-semibold text-blue-600">{parseJiraKey(form.jira_url)}</span>
                        : <span className="text-gray-400 italic">could not extract — you can also type the key directly (e.g. RUPAY)</span>}
                    </p>
                  )}
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">Confluence Space URL</label>
                  <input
                    value={form.confluence_url}
                    onChange={e => handleChange("confluence_url", e.target.value)}
                    onBlur={() => handleBlur("confluence_url")}
                    className={`w-full bg-white border rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${formErrors.confluence_url ? "border-red-500" : "border-gray-300"}`}
                    placeholder="https://ainxt.atlassian.net/wiki/spaces/RUPAY/overview"
                  />
                  {formErrors.confluence_url && (
                    <p className="mt-1 text-xs text-red-600">{formErrors.confluence_url}</p>
                  )}
                  {form.confluence_url && !formErrors.confluence_url && (
                    <p className="mt-1 text-xs text-gray-400">
                      Space key:{" "}
                      {parseConfluenceSpace(form.confluence_url)
                        ? <span className="font-mono font-semibold text-blue-600">{parseConfluenceSpace(form.confluence_url)}</span>
                        : <span className="text-gray-400 italic">could not extract — you can also type the key directly (e.g. RUPAY)</span>}
                    </p>
                  )}
                </div>
              </div>

              {/* Repos */}
              <div>
                <label className="block text-xs text-gray-500 mb-1">Repositories <span className="text-gray-400">(optional — add now or later)</span></label>
                <div className="flex gap-2 mb-2">
                  <input
                    type="text"
                    value={repoCreateInput}
                    onChange={e => handleRepoInputChange(e.target.value)}
                    onBlur={handleRepoInputBlur}
                    onKeyDown={e => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        const error = validateField("repoInput", repoCreateInput);
                        if (error) {
                          setFormErrors(prev => ({ ...prev, repoInput: error }));
                          return;
                        }
                        const v = repoCreateInput.trim();
                        if (v && !form.repos.find(r => r.repo_name === v)) {
                          setForm(f => ({ ...f, repos: [...f.repos, { repo_name: v, branch: "main" }] }));
                          setRepoCreateInput("");
                        }
                      }
                    }}
                    placeholder="org/repo-name (press Enter to add)"
                    className={`flex-1 bg-white border rounded px-3 py-1.5 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${formErrors.repoInput ? "border-red-500" : "border-gray-300"}`}
                  />
                  <button
                    type="button"
                    onClick={() => {
                      const error = validateField("repoInput", repoCreateInput);
                      if (error) {
                        setFormErrors(prev => ({ ...prev, repoInput: error }));
                        return;
                      }
                      const v = repoCreateInput.trim();
                      if (v && !form.repos.find(r => r.repo_name === v)) {
                        setForm(f => ({ ...f, repos: [...f.repos, { repo_name: v, branch: "main" }] }));
                        setRepoCreateInput("");
                      }
                    }}
                    className="cursor-pointer px-3 py-1.5 brand-grad hover:opacity-70 rounded text-sm text-white transition-colors cursor-pointer focus:outline-none rounded text-sm text-gray-700 transition-colors"
                  >
                    Add
                  </button>
                </div>
                {formErrors.repoInput && (
                  <p className="mt-1 text-xs text-red-600 mb-2">{formErrors.repoInput}</p>
                )}
                {form.repos.length > 0 && (
                  <div className="space-y-1">
                    {form.repos.map(r => (
                      <div key={r.repo_name} className="flex items-center justify-between bg-gray-50 border border-gray-100 rounded px-3 py-1.5 text-sm">
                        <span className="font-mono text-gray-700">{r.repo_name}</span>
                        <button
                          type="button"
                          onClick={() => setForm(f => ({ ...f, repos: f.repos.filter(x => x.repo_name !== r.repo_name) }))}
                          className="text-xs text-red-400 hover:text-red-600 ml-2"
                        >
                          ×
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="flex gap-2 pt-2">
                <button
                  type="submit"
                  disabled={saving}
                  className="px-4 py-2 cursor-pointer disabled:opacity-50 rounded text-sm font-medium text-white transition-colors brand-grad hover:opacity-70"
                >
                  {saving ? "Saving…" : isAdmin ? "Create Product" : "Submit for Approval"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setCreating(false);
                    setForm({ name: "", code: "", description: "", jira_url: "", confluence_url: "", departments: [], repos: [] });
                    setFormErrors({ name: "", code: "", description: "", jira_url: "", confluence_url: "", departments: "", repoInput: "" });
                    setRepoCreateInput("");
                    setError("");
                    setSuccess("");
                  }}
                  className="px-4 py-2 bg-white border border-gray-300 hover:bg-gray-100 rounded text-sm text-gray-700 transition-colors cursor-pointer"
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        )}

        {/* Product detail */}
        {selected && !creating && (
          <div className="max-w-3xl space-y-5">
            {/* Header */}
            <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
              <div className="flex items-start justify-between">
                <div>
                  <h2 className="text-xl font-bold text-gray-900">{selected.name}</h2>
                  <span className="text-xs font-mono bg-gray-100 text-gray-500 px-2 py-0.5 rounded mt-1 inline-block">{selected.code}</span>
                </div>
                {/* Action buttons: Edit + Delete for approvers/admin */}
                {canApprove && (
                  <div className="flex gap-2">
                    <button
                      onClick={() => {
                        setEditing(e => !e);
                        setEditForm({
                          jira_url:       selected.jira_url       || "",
                          confluence_url: selected.confluence_url || "",
                        });
                      }}
                      className="cursor-pointer flex items-center gap-1.5 px-3 py-1.5 text-white rounded text-sm brand-grad hover:opacity-70 transition-colors"
                    >
                      <Pencil size={12} /> Edit URLs
                    </button>
                    <button
                      onClick={deleteProduct}
                      className="cursor-pointer flex items-center gap-1.5 px-3 py-1.5 rounded text-sm text-red-500 hover:bg-red-50 transition-colors"
                    >
                      <Trash2 size={12} /> Delete
                    </button>
                  </div>
                )}
              </div>
              {selected.description && (
                <p className="text-sm text-gray-500 mt-3">{selected.description}</p>
              )}

              {/* Inline URL edit form */}
              {editing ? (
                <div className="mt-4 p-4 bg-gray-50 border border-gray-200 rounded-lg space-y-3">
                  <p className="text-xs font-medium text-gray-600">Edit Integration URLs</p>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-gray-500 mb-1">Jira Project URL</label>
                      <input
                        value={editForm.jira_url}
                        onChange={e => handleEditChange("jira_url", e.target.value)}
                        onBlur={() => handleEditBlur("jira_url")}
                        className={`w-full bg-white border rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${editFormErrors.jira_url ? "border-red-500" : "border-gray-300"}`}
                        placeholder="https://ainxt.atlassian.net/jira/software/projects/…"
                      />
                      {editFormErrors.jira_url && (
                        <p className="mt-1 text-xs text-red-600">{editFormErrors.jira_url}</p>
                      )}
                      {!editFormErrors.jira_url && editForm.jira_url && (
                        <p className="mt-1 text-xs text-gray-400">
                          Key: {parseJiraKey(editForm.jira_url)
                            ? <span className="font-mono text-blue-600">{parseJiraKey(editForm.jira_url)}</span>
                            : <span className="italic">could not extract — type the key directly (e.g. RUPAY)</span>}
                        </p>
                      )}
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 mb-1">Confluence Space URL</label>
                      <input
                        value={editForm.confluence_url}
                        onChange={e => handleEditChange("confluence_url", e.target.value)}
                        onBlur={() => handleEditBlur("confluence_url")}
                        className={`w-full bg-white border rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${editFormErrors.confluence_url ? "border-red-500" : "border-gray-300"}`}
                        placeholder="https://ainxt.atlassian.net/wiki/spaces/…"
                      />
                      {editFormErrors.confluence_url && (
                        <p className="mt-1 text-xs text-red-600">{editFormErrors.confluence_url}</p>
                      )}
                      {!editFormErrors.confluence_url && editForm.confluence_url && (
                        <p className="mt-1 text-xs text-gray-400">
                          Space: {parseConfluenceSpace(editForm.confluence_url)
                            ? <span className="font-mono text-blue-600">{parseConfluenceSpace(editForm.confluence_url)}</span>
                            : <span className="italic">could not extract — type the key directly (e.g. RUPAY)</span>}
                        </p>
                      )}
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={saveEdit}
                      disabled={editSaving}
                      className="cursor-pointer px-3 py-1.5 brand-grad hover:opacity-70 rounded text-sm text-white transition-colors"
                    >
                      {editSaving ? "Saving…" : "Save"}
                    </button>
                    <button
                      onClick={() => {
                        setEditing(false);
                        setEditForm({ jira_url: "", confluence_url: "" });
                        setEditFormErrors({ jira_url: "", confluence_url: "" });
                        setError( "" );
                        setSuccess( "" );
                      }}
                      className="cursor-pointer px-3 py-1.5 bg-white border border-gray-300 hover:bg-gray-100 rounded text-sm text-gray-700 transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-3 mt-3 text-sm">
                  {selected.jira_project_key && (
                    <div>
                      <span className="text-gray-400">Jira Project</span>
                      <p className="font-mono font-semibold text-gray-800 mt-0.5">{selected.jira_project_key}</p>
                      {selected.jira_url && (
                        <a href={selected.jira_url} target="_blank" rel="noopener noreferrer"
                          className="text-xs text-blue-500 hover:underline break-all">{selected.jira_url}</a>
                      )}
                    </div>
                  )}
                  {selected.confluence_space && (
                    <div>
                      <span className="text-gray-400">Confluence Space</span>
                      <p className="font-mono font-semibold text-gray-800 mt-0.5">{selected.confluence_space}</p>
                      {selected.confluence_url && (
                        <a href={selected.confluence_url} target="_blank" rel="noopener noreferrer"
                          className="text-xs text-blue-500 hover:underline break-all">{selected.confluence_url}</a>
                      )}
                    </div>
                  )}
                  {!selected.jira_project_key && !selected.confluence_space && canApprove && (
                    <p className="text-xs text-gray-400 col-span-2">No Jira or Confluence URLs set. Click "Edit URLs" to add them.</p>
                  )}
                </div>
              )}

              {/* Departments */}
              {(selected.departments || []).length > 0 && (
                <div className="mt-3">
                  <span className="text-xs text-gray-400">Departments</span>
                  <div className="flex flex-wrap gap-1.5 mt-1">
                    {selected.departments.map(d => (
                      <span key={d} className="px-2 py-0.5 bg-blue-50 border border-blue-200 text-blue-700 rounded-full text-xs">{d}</span>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Repos */}
            <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
              <h3 className="font-semibold text-gray-800 mb-3">Repositories</h3>
              <div className="space-y-1 mb-3">
                {(selected.repos || []).length === 0 && (
                  <p className="text-sm text-gray-400">No repos linked.</p>
                )}
                {(selected.repos || []).map(r => (
                  <div key={r.repo_name} className="flex items-center justify-between bg-gray-50 border border-gray-100 rounded px-3 py-2 text-sm">
                    <span className="text-gray-800 font-mono">{r.repo_name}</span>
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-gray-400">{r.branch}</span>
                      <button onClick={() => removeRepo(r.repo_name)} className="text-xs text-red-400 hover:text-red-600">×</button>
                    </div>
                  </div>
                ))}
              </div>
              <div>
                <div className="flex gap-2">
                  <input
                    value={repoInput}
                    onChange={e => { setRepoInput(e.target.value); setRepoInputError(""); }}
                    onBlur={() => {
                      if (repoInput.trim()) {
                        const r = validateRepoName(repoInput.trim());
                        setRepoInputError(r.isValid ? "" : r.errors[0]?.message || "");
                      } else {
                        setRepoInputError("");
                      }
                    }}
                    placeholder="org/repo-name"
                    className={`flex-1 bg-white border rounded px-3 py-1.5 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${repoInputError ? "border-red-500" : "border-gray-300"}`}
                  />
                  <button onClick={addRepo} className="px-3 py-1.5 brand-grad hover:opacity-70 rounded text-sm text-white transition-colors cursor-pointer">
                    Add
                  </button>
                </div>
                {repoInputError && <p className="mt-1 text-xs text-red-600">{repoInputError}</p>}
              </div>
            </div>

            {/* People with Access — live from org_tree by department */}
            <PeopleWithAccess people={selected.people || []} />
          </div>
        )}

        {!selected && !creating && (
          <div className="text-gray-400 text-sm">
            Select a product from the list{canCreate ? " or create a new one" : ""}.
          </div>
        )}
      </div>
    </div>
  );
}
