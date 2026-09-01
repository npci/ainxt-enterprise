// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect } from "react";
import { usePermission } from "../hooks/usePermission";
import { API_BASE } from "../config";
import { toISTDate } from "../utils/time";

const ENTITY_TYPES = ["agents", "skills", "workflows", "mcp"];

const STATUS_COLORS = {
  DRAFT:            "bg-gray-100 text-gray-600",
  PENDING_APPROVAL: "bg-yellow-100 text-yellow-700",
  APPROVED:         "bg-blue-100 text-blue-700",
  PRODUCTION:       "bg-green-100 text-green-700",
  REJECTED:         "bg-red-100 text-red-700",
  DEPRECATED:       "bg-gray-200 text-gray-500",
};

const APPROVER_DOMAINS = ["IS", "EA", "DPDP"];

// Approver-form validation (client-side UX only — server is the real gate)
const APPROVER_DOMAIN_RE = /^[A-Z][A-Z0-9_]*$/;
const APPROVER_EMAIL_RE  = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ── URL path-segment allow-list (SSRF / path-injection guard) ───────────────
//
// Every dynamic value interpolated into a fetch() URL in this file is a
// database-generated id, an action verb, or an entity-type name — never a
// full URL, host, or scheme. This positive allow-list regex is a FULL-STRING
// match (\A...\Z semantics via ^...$ with no multiline flag): the value must
// be composed ENTIRELY of [a-zA-Z0-9_-] characters and be 1-100 of them.
//
// Unlike a strip-and-continue approach (`.replace(/[^...]/g, '')`), a value
// that fails this test is REJECTED outright — the request is never sent, not
// even with a mangled/stripped value. This is a whitelist, not a cleanup:
// every character used to alter a URL's structure (`/`, `:`, `.`, `\`, `?`,
// `#`, `@`, whitespace) is outside the allowed set, so no such value can ever
// reach fetch(), regardless of where it originated (including a value read
// back from a prior API response — the second-order case).
const SAFE_PATH_SEGMENT_RE = /^[a-zA-Z0-9_-]{1,100}$/;

// Suppression source labels shown in the table/filter
const SUPPRESSION_SOURCE_LABELS = {
  in_pipeline: "In pipeline",
  uploaded:    "Uploaded",
  prior_run:   "Prior run",
};

// --- Bulk suppression upload parsing (CSV or JSON) -------------------------
// CSV columns: skill,fingerprint  OR  skill,file,rule,snippet,title (+ optional reason)
function parseSuppressionCsv(text) {
  const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
  if (lines.length === 0) return [];
  const header = lines[0].split(",").map(h => h.trim().toLowerCase());
  return lines.slice(1).map(line => {
    const cells = line.split(",").map(c => c.trim());
    const obj = {};
    header.forEach((h, i) => { if (h) obj[h] = cells[i] ?? ""; });
    return obj;
  });
}

// Accepts JSON (array of items, or { items: [...] }) or CSV text; throws
// with a user-facing message on parse failure.
function parseBulkSuppressionInput(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) throw new Error("Paste or upload CSV/JSON content first.");
  if (trimmed.startsWith("[") || trimmed.startsWith("{")) {
    let parsed;
    try {
      parsed = JSON.parse(trimmed);
    } catch (e) {
      throw new Error(`Invalid JSON: ${e.message}`);
    }
    const arr = Array.isArray(parsed) ? parsed : (Array.isArray(parsed?.items) ? parsed.items : null);
    if (!arr) throw new Error("JSON must be an array of items, or { items: [...] }.");
    return arr;
  }
  const rows = parseSuppressionCsv(trimmed);
  if (rows.length === 0) throw new Error("No rows found in CSV — expected a header row plus data.");
  return rows;
}

export default function Governance({ user }) {
  const { can, isAdmin, canApprove } = usePermission(user);
  const [items,        setItems]       = useState([]);
  const [entityType,   setEntityType]  = useState("agents");
  const [filterStatus, setFilter]      = useState("ALL");
  const [loading,      setLoading]     = useState(false);
  const [rejectModal,  setRejectModal] = useState(null);
  const [rejectReason, setRejectReason]= useState("");

  // Domain approvers state (admin-only)
  const [approvers,        setApprovers]        = useState([]);
  const [approversLoading, setApproversLoading] = useState(false);
  const [approverForm,     setApproverForm]     = useState({
    domain: "IS", customDomain: "", email: "", user_id: "",
  });
  const [approverAdding,   setApproverAdding]   = useState(false);
  const [approverError,    setApproverError]    = useState("");

  // Suppression management state (B3.2)
  const [suppressions,        setSuppressions]        = useState([]);
  const [suppressionsLoading, setSuppressionsLoading] = useState(false);
  const [suppressionSearch,   setSuppressionSearch]   = useState("");
  const [suppressionRowError, setSuppressionRowError] = useState("");

  // Bulk suppression upload widget state
  const [bulkRepo,        setBulkRepo]        = useState("");
  const [bulkProductId,   setBulkProductId]   = useState("");
  const [bulkSource,      setBulkSource]      = useState("prior_run");
  const [bulkText,        setBulkText]        = useState("");
  const [bulkSubmitting,  setBulkSubmitting]  = useState(false);
  const [bulkError,       setBulkError]       = useState("");
  const [bulkResult,      setBulkResult]      = useState("");

  const headers = { "Content-Type": "application/json" };

  const load = () => {
    setLoading(true);
    const govListUrl = `${API_BASE}/governance/${entityType}`;
    fetch(govListUrl, { headers, credentials: "include" })
      .then(govListRes => { if (!govListRes.ok) throw new Error(govListRes.status); return govListRes.json(); })
      .then(govListData => {
        const raw = Array.isArray(govListData) ? govListData : (govListData.items || []);
        setItems(raw.map(item => ({
          ...item,
          name: String(item.name || '').replace(/[^a-zA-Z0-9_\-]/g, ''),
        })));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(load, [entityType]);

  const action = async (name, verb, body = {}) => {
    // API_BASE is a compile-time constant (never user-supplied). Each dynamic
    // segment is validated INLINE, in this scope, against a positive
    // allow-list (SAFE_PATH_SEGMENT_RE) immediately before use — a value that
    // does not match in full is REJECTED, not stripped-and-continued. No host
    // or path injection is possible. No SSRF vector.
    const rawName = String(name);
    const rawVerb = String(verb);
    if (!SAFE_PATH_SEGMENT_RE.test(rawName) || !SAFE_PATH_SEGMENT_RE.test(rawVerb)) return;
    const safeName = rawName;
    const safeVerb = rawVerb;
    const safeEntityType = ENTITY_TYPES.includes(entityType) ? entityType : '';
    if (!safeEntityType) return;
    const govActionUrl = `${API_BASE}/governance/${safeEntityType}/${safeName}/${safeVerb}`;
    await fetch(govActionUrl, {
      method: "POST", headers, credentials: "include", body: JSON.stringify(body),
    });
    load();
  };

  // Domain approvers helpers
  const loadApprovers = () => {
    setApproversLoading(true);
    fetch(`${API_BASE}/sdlc/governance/domain-approvers`, { headers, credentials: "include" })
      .then(r => r.json())
      .then(d => setApprovers(Array.isArray(d) ? d : []))
      .catch(() => {})
      .finally(() => setApproversLoading(false));
  };

  useEffect(() => { if (isAdmin) loadApprovers(); }, []);

  const resolvedDomain = approverForm.domain === "Custom"
    ? approverForm.customDomain.trim().toUpperCase()
    : approverForm.domain;

  const addApprover = async () => {
    setApproverError("");
    if (!resolvedDomain) { setApproverError("Domain is required."); return; }
    if (!APPROVER_DOMAIN_RE.test(resolvedDomain)) {
      setApproverError("Domain must be an uppercase token (letters/numbers/underscore, e.g. IS, EA, DPDP).");
      return;
    }
    const approverEmail = approverForm.email.trim();
    if (!approverEmail) { setApproverError("Email is required."); return; }
    if (!APPROVER_EMAIL_RE.test(approverEmail)) {
      setApproverError("Enter a valid email address.");
      return;
    }
    setApproverAdding(true);
    try {
      const body = {
        domain: resolvedDomain,
        approver_email: approverEmail,
        ...(approverForm.user_id.trim() ? { approver_user_id: approverForm.user_id.trim() } : {}),
      };
      const r = await fetch(`${API_BASE}/sdlc/governance/domain-approvers`, {
        method: "POST", headers, credentials: "include", body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setApproverError(err.detail || "Failed to add approver.");
      } else {
        setApproverForm({ domain: "IS", customDomain: "", email: "", user_id: "" });
        loadApprovers();
      }
    } catch {
      setApproverError("Network error.");
    } finally {
      setApproverAdding(false);
    }
  };

  const removeApprover = async (id) => {
    // id is validated INLINE, in this scope, against a positive allow-list
    // (SAFE_PATH_SEGMENT_RE) immediately before use — a value that does not
    // match in full is REJECTED, not stripped-and-continued. No
    // path-traversal or host-injection is possible. No SSRF vector.
    const rawId = String(id);
    if (!SAFE_PATH_SEGMENT_RE.test(rawId)) return;
    const safeId = rawId;
    await fetch(`${API_BASE}/sdlc/governance/domain-approvers/${safeId}`, {
      method: "DELETE", headers, credentials: "include",
    }).catch(() => {});
    loadApprovers();
  };

  // Suppression management helpers (B3.2)
  const loadSuppressions = () => {
    setSuppressionsLoading(true);
    fetch(`${API_BASE}/sdlc/governance-suppressions`, { headers, credentials: "include" })
      .then(r => r.json())
      .then(d => setSuppressions(Array.isArray(d?.suppressions) ? d.suppressions : (Array.isArray(d) ? d : [])))
      .catch(() => {})
      .finally(() => setSuppressionsLoading(false));
  };

  useEffect(loadSuppressions, []);

  const deleteSuppression = async (id) => {
    setSuppressionRowError("");
    // id is validated INLINE, in this scope, against a positive allow-list
    // (SAFE_PATH_SEGMENT_RE) immediately before use — a value that does not
    // match in full is REJECTED, not stripped-and-continued. No
    // path-traversal or host-injection is possible. No SSRF vector.
    const rawId = String(id);
    if (!SAFE_PATH_SEGMENT_RE.test(rawId)) { setSuppressionRowError("Invalid suppression id."); return; }
    const safeId = rawId;
    try {
      const r = await fetch(`${API_BASE}/sdlc/governance-suppressions/${safeId}`, {
        method: "DELETE", headers, credentials: "include",
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setSuppressionRowError(err.detail || "Failed to delete suppression.");
        return;
      }
      loadSuppressions();
    } catch {
      setSuppressionRowError("Network error.");
    }
  };

  const signoffSuppression = async (id) => {
    setSuppressionRowError("");
    // id is validated INLINE, in this scope, against a positive allow-list
    // (SAFE_PATH_SEGMENT_RE) immediately before use — a value that does not
    // match in full is REJECTED, not stripped-and-continued. No
    // path-traversal or host-injection is possible. No SSRF vector.
    const rawId = String(id);
    if (!SAFE_PATH_SEGMENT_RE.test(rawId)) { setSuppressionRowError("Invalid suppression id."); return; }
    const safeId = rawId;
    try {
      const r = await fetch(`${API_BASE}/sdlc/governance-suppressions/${safeId}/signoff`, {
        method: "POST", headers, credentials: "include",
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setSuppressionRowError(err.detail || "Failed to sign off suppression.");
        return;
      }
      loadSuppressions();
    } catch {
      setSuppressionRowError("Network error.");
    }
  };

  // Bulk upload helpers
  const handleBulkFile = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setBulkText(String(reader.result || ""));
    reader.readAsText(file);
  };

  const submitBulkSuppressions = async () => {
    setBulkError("");
    setBulkResult("");

    let items;
    try {
      items = parseBulkSuppressionInput(bulkText);
    } catch (e) {
      setBulkError(e.message || "Could not parse input.");
      return;
    }
    if (!bulkRepo.trim()) { setBulkError("Repo is required."); return; }
    if (!Array.isArray(items) || items.length === 0) { setBulkError("No items found in input."); return; }

    const badIdx = items.findIndex(it => !it || !String(it.skill || "").trim());
    if (badIdx !== -1) {
      setBulkError(`Item ${badIdx + 1} is missing "skill".`);
      return;
    }

    setBulkSubmitting(true);
    try {
      const body = {
        repo: bulkRepo.trim(),
        ...(bulkProductId.trim() ? { product_id: bulkProductId.trim() } : {}),
        ...(bulkSource.trim() ? { source: bulkSource.trim() } : {}),
        items: items.map(it => ({
          skill:       String(it.skill).trim(),
          fingerprint: it.fingerprint ? String(it.fingerprint).trim() : undefined,
          file:        it.file ? String(it.file).trim() : undefined,
          rule:        it.rule ? String(it.rule).trim() : undefined,
          snippet:     it.snippet ? String(it.snippet).trim() : undefined,
          title:       it.title ? String(it.title).trim() : undefined,
          reason:      it.reason ? String(it.reason).trim() : undefined,
        })),
      };
      const r = await fetch(`${API_BASE}/sdlc/governance-suppressions/bulk`, {
        method: "POST", headers, credentials: "include", body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setBulkError(err.detail || "Bulk upload failed.");
        return;
      }
      const data = await r.json().catch(() => ({}));
      const inserted = typeof data.inserted === "number" ? data.inserted : items.length;
      setBulkResult(`Uploaded ${inserted} row(s) — these are pending sign-off and won't suppress findings until a governance lead signs off.`);
      setBulkText("");
      loadSuppressions();
    } catch {
      setBulkError("Network error.");
    } finally {
      setBulkSubmitting(false);
    }
  };

  const filteredSuppressions = suppressionSearch.trim()
    ? suppressions.filter(s => {
        const q = suppressionSearch.trim().toLowerCase();
        const repoName = (s.repo_name || s.repo || "").toLowerCase();
        const skill    = (s.skill || "").toLowerCase();
        const source   = (s.source || "").toLowerCase();
        return repoName.includes(q) || skill.includes(q) || source.includes(q);
      })
    : suppressions;

  const filtered = filterStatus === "ALL"
    ? items
    : items.filter(i => i.status === filterStatus);

  // Developers see only their own; security+ see all
  const visible = can("security")
    ? filtered
    : filtered.filter(i => i.created_by === user?.userId || i.owner === user?.email);

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-xl font-semibold text-gray-800 mb-1">Governance</h1>
      <p className="text-xs text-gray-400 mb-4">
        {can("security") ? "Showing all submissions" : "Showing your submissions"}
      </p>

      {/* Entity type tabs */}
      <div className="flex gap-2 mb-3 flex-wrap">
        {ENTITY_TYPES.map(e => (
          <button key={e} onClick={() => setEntityType(e)}
            className={`px-3 py-1 rounded-full text-sm capitalize transition ${
              entityType === e ? "bg-gray-800 text-white" : "bg-gray-100 text-gray-600 hover:bg-gray-200"
            }`}>{e}</button>
        ))}
        <div className="flex-1" />
        {/* Status filters */}
        {["ALL","DRAFT","PENDING_APPROVAL","APPROVED","PRODUCTION","REJECTED"].map(s => (
          <button key={s} onClick={() => setFilter(s)}
            className={`px-2 py-1 rounded-full text-xs transition ${
              filterStatus === s ? "bg-gray-700 text-white" : "bg-gray-100 text-gray-500 hover:bg-gray-200"
            }`}>{s.replace(/_/g," ")}</button>
        ))}
      </div>

      {/* Table */}
      <div className="border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
            <tr>
              <th className="px-4 py-3 text-left">Name</th>
              <th className="px-4 py-3 text-left">Status</th>
              <th className="px-4 py-3 text-left">Submitted by</th>
              <th className="px-4 py-3 text-left">Updated</th>
              <th className="px-4 py-3 text-left">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {loading ? (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-gray-400 text-sm">Loading…</td></tr>
            ) : visible.length === 0 ? (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-gray-400 text-sm">No items</td></tr>
            ) : visible.map(item => (
              <tr key={item.name} className="hover:bg-gray-50">
                <td className="px-4 py-3 font-medium text-gray-800">{item.name}</td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[item.status] || ""}`}>
                    {(item.status || "").replace(/_/g," ")}
                  </span>
                  {item.rejection_reason && (
                    <p className="text-xs text-red-500 mt-0.5 max-w-xs truncate" title={item.rejection_reason}>
                      ↳ {item.rejection_reason}
                    </p>
                  )}
                </td>
                <td className="px-4 py-3 text-gray-500 text-xs">{item.created_by || "—"}</td>
                <td className="px-4 py-3 text-gray-400 text-xs">{toISTDate(item.updated_at) || "—"}</td>
                <td className="px-4 py-3">
                  <div className="flex gap-1.5 flex-wrap">

                    {/* Maker: submit draft or rejected item */}
                    {["DRAFT","REJECTED"].includes(item.status) && can("developer") && (
                      <button onClick={() => action(item.name, "submit")}
                        className="px-2 py-1 bg-blue-600 text-white text-xs rounded-md hover:bg-blue-700">
                        Submit
                      </button>
                    )}

                    {/* Checker: approve or reject pending items */}
                    {item.status === "PENDING_APPROVAL" && can("security") && (
                      <>
                        <button onClick={() => action(item.name, "approve")}
                          className="px-2 py-1 bg-green-600 text-white text-xs rounded-md hover:bg-green-700">
                          Approve
                        </button>
                        <button onClick={() => { setRejectModal({ name: item.name }); setRejectReason(""); }}
                          className="px-2 py-1 bg-red-100 text-red-600 text-xs rounded-md hover:bg-red-200">
                          Reject
                        </button>
                      </>
                    )}

                    {/* Admin: promote approved to production */}
                    {item.status === "APPROVED" && can("admin") && (
                      <button onClick={() => action(item.name, "promote")}
                        className="px-2 py-1 bg-purple-600 text-white text-xs rounded-md hover:bg-purple-700">
                        → PROD
                      </button>
                    )}

                    {/* Admin: deprecate */}
                    {item.status === "PRODUCTION" && can("admin") && (
                      <button onClick={() => action(item.name, "deprecate")}
                        className="px-2 py-1 bg-gray-200 text-gray-600 text-xs rounded-md hover:bg-gray-300">
                        Deprecate
                      </button>
                    )}

                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Suppression Management — visible to anyone with visibility into the
          underlying repo/product (server scopes the list); sign-off is
          restricted to admins/approvers below. */}
      <div className="mt-8">
        <h2 className="text-base font-semibold text-gray-800 mb-0.5">Governance Suppressions</h2>
        <p className="text-xs text-gray-400 mb-4">
          False-positive suppressions for governance findings. Bulk-uploaded rows stay
          inert (pending sign-off) until a governance lead or admin approves them.
        </p>

        {/* Bulk upload widget */}
        <div className="border border-gray-200 rounded-lg p-4 mb-4 bg-gray-50">
          <p className="text-xs font-medium text-gray-600 mb-3 uppercase tracking-wide">Bulk Upload (CSV or JSON)</p>
          <div className="flex gap-2 flex-wrap items-end mb-3">
            <div className="flex flex-col gap-1">
              <label className="text-xs text-gray-500">Repo</label>
              <input
                type="text"
                value={bulkRepo}
                onChange={e => setBulkRepo(e.target.value)}
                placeholder="group/repo"
                className="border border-gray-200 rounded-lg px-3 py-2 text-sm w-44 focus:outline-none focus:ring-2 focus:ring-gray-300"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs text-gray-500">Product ID (optional)</label>
              <input
                type="text"
                value={bulkProductId}
                onChange={e => setBulkProductId(e.target.value)}
                placeholder="product id"
                className="border border-gray-200 rounded-lg px-3 py-2 text-sm w-40 focus:outline-none focus:ring-2 focus:ring-gray-300"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs text-gray-500">Source</label>
              <select
                value={bulkSource}
                onChange={e => setBulkSource(e.target.value)}
                className="border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-gray-300"
              >
                <option value="prior_run">prior_run</option>
                <option value="uploaded">uploaded</option>
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs text-gray-500">Or upload a file</label>
              <input
                type="file"
                accept=".csv,.json,text/csv,application/json"
                onChange={handleBulkFile}
                className="text-xs"
              />
            </div>
          </div>

          <textarea
            value={bulkText}
            onChange={e => setBulkText(e.target.value)}
            placeholder={"CSV: skill,fingerprint\nor: skill,file,rule,snippet,title\n\nor JSON: [ { \"skill\": \"...\", \"fingerprint\": \"...\" } ]"}
            className="w-full border border-gray-200 rounded-lg p-3 text-xs font-mono h-28 resize-none focus:outline-none focus:ring-2 focus:ring-gray-300"
          />

          <div className="flex items-center gap-3 mt-3">
            <button
              onClick={submitBulkSuppressions}
              disabled={bulkSubmitting}
              className="px-4 py-2 bg-gray-800 text-white text-sm rounded-lg hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {bulkSubmitting ? "Uploading…" : "Upload"}
            </button>
            {bulkError && <span className="text-xs text-red-500">{bulkError}</span>}
            {bulkResult && !bulkError && <span className="text-xs text-green-600">{bulkResult}</span>}
          </div>
        </div>

        {/* Search/filter */}
        <div className="mb-3">
          <input
            type="text"
            value={suppressionSearch}
            onChange={e => setSuppressionSearch(e.target.value)}
            placeholder="Filter by repo, skill, or source…"
            className="border border-gray-200 rounded-lg px-3 py-2 text-sm w-72 focus:outline-none focus:ring-2 focus:ring-gray-300"
          />
        </div>

        {suppressionRowError && (
          <p className="text-xs text-red-500 mb-2">{suppressionRowError}</p>
        )}

        {/* Suppressions table */}
        <div className="border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
              <tr>
                <th className="px-4 py-3 text-left">Repo</th>
                <th className="px-4 py-3 text-left">Product</th>
                <th className="px-4 py-3 text-left">Skill</th>
                <th className="px-4 py-3 text-left">Source</th>
                <th className="px-4 py-3 text-left">Status</th>
                <th className="px-4 py-3 text-left">Created by</th>
                <th className="px-4 py-3 text-left">Created</th>
                <th className="px-4 py-3 text-left">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {suppressionsLoading ? (
                <tr><td colSpan={8} className="px-4 py-8 text-center text-gray-400 text-sm">Loading…</td></tr>
              ) : filteredSuppressions.length === 0 ? (
                <tr><td colSpan={8} className="px-4 py-8 text-center text-gray-400 text-sm">No suppressions</td></tr>
              ) : filteredSuppressions.map(s => (
                <tr key={s.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-gray-700 text-xs font-mono">{s.repo_name || s.repo || "—"}</td>
                  <td className="px-4 py-3 text-gray-500 text-xs">{s.product_id || "—"}</td>
                  <td className="px-4 py-3 text-gray-700 text-xs">{s.skill || "—"}</td>
                  <td className="px-4 py-3 text-gray-500 text-xs">
                    {SUPPRESSION_SOURCE_LABELS[s.source] || s.source || "—"}
                  </td>
                  <td className="px-4 py-3">
                    {s.pending_signoff ? (
                      <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-700">
                        Pending sign-off
                      </span>
                    ) : (
                      <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-700">
                        Active
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">{s.created_by || "—"}</td>
                  <td className="px-4 py-3 text-gray-400 text-xs">{toISTDate(s.created_at) || "—"}</td>
                  <td className="px-4 py-3">
                    <div className="flex gap-1.5 flex-wrap">
                      {s.pending_signoff && (can("admin") || canApprove) && (
                        <button
                          onClick={() => signoffSuppression(s.id)}
                          className="px-2 py-1 bg-blue-600 text-white text-xs rounded-md hover:bg-blue-700"
                        >
                          Sign off
                        </button>
                      )}
                      <button
                        onClick={() => deleteSuppression(s.id)}
                        className="px-2 py-1 bg-red-100 text-red-600 text-xs rounded-md hover:bg-red-200"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Governance Domain Approvers — admin only. Gated on isAdmin (equivalent
          to can("admin")) so non-admins never see approver controls/emails. */}
      {isAdmin && (
        <div className="mt-8">
          <h2 className="text-base font-semibold text-gray-800 mb-0.5">Governance Domain Approvers</h2>
          <p className="text-xs text-gray-400 mb-4">
            Controls which users can approve governance findings for each domain (IS, EA, DPDP).
          </p>

          {/* Add approver form */}
          <div className="border border-gray-200 rounded-lg p-4 mb-4 bg-gray-50">
            <p className="text-xs font-medium text-gray-600 mb-3 uppercase tracking-wide">Add Approver</p>
            <div className="flex gap-2 flex-wrap items-end">

              {/* Domain dropdown */}
              <div className="flex flex-col gap-1">
                <label className="text-xs text-gray-500">Domain</label>
                <select
                  value={approverForm.domain}
                  onChange={e => setApproverForm(f => ({ ...f, domain: e.target.value, customDomain: "" }))}
                  className="border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-gray-300"
                >
                  {APPROVER_DOMAINS.map(d => (
                    <option key={d} value={d}>{d}</option>
                  ))}
                  <option value="Custom">Custom…</option>
                </select>
              </div>

              {/* Custom domain text input */}
              {approverForm.domain === "Custom" && (
                <div className="flex flex-col gap-1">
                  <label className="text-xs text-gray-500">Custom Domain</label>
                  <input
                    type="text"
                    value={approverForm.customDomain}
                    onChange={e => setApproverForm(f => ({ ...f, customDomain: e.target.value }))}
                    placeholder="e.g. SECURITY"
                    className="border border-gray-200 rounded-lg px-3 py-2 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-gray-300"
                  />
                </div>
              )}

              {/* Email */}
              <div className="flex flex-col gap-1 flex-1 min-w-[200px]">
                <label className="text-xs text-gray-500">Approver Email</label>
                <input
                  type="email"
                  value={approverForm.email}
                  onChange={e => setApproverForm(f => ({ ...f, email: e.target.value }))}
                  placeholder="user@example.com"
                  className="border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-gray-300"
                />
              </div>

              {/* User ID (optional) */}
              <div className="flex flex-col gap-1 w-36">
                <label className="text-xs text-gray-500">User ID (optional)</label>
                <input
                  type="text"
                  value={approverForm.user_id}
                  onChange={e => setApproverForm(f => ({ ...f, user_id: e.target.value }))}
                  placeholder="uid or ldap"
                  className="border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-gray-300"
                />
              </div>

              {/* Submit */}
              <button
                onClick={addApprover}
                disabled={approverAdding}
                className="px-4 py-2 bg-gray-800 text-white text-sm rounded-lg hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed whitespace-nowrap"
              >
                {approverAdding ? "Adding…" : "Add Approver"}
              </button>
            </div>

            {approverError && (
              <p className="text-xs text-red-500 mt-2">{approverError}</p>
            )}
          </div>

          {/* Approvers table */}
          <div className="border border-gray-200 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
                <tr>
                  <th className="px-4 py-3 text-left">Domain</th>
                  <th className="px-4 py-3 text-left">Email</th>
                  <th className="px-4 py-3 text-left">User ID</th>
                  <th className="px-4 py-3 text-left">Added By</th>
                  <th className="px-4 py-3 text-left">Added At</th>
                  <th className="px-4 py-3 text-left">Remove</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {approversLoading ? (
                  <tr>
                    <td colSpan={6} className="px-4 py-8 text-center text-gray-400 text-sm">Loading…</td>
                  </tr>
                ) : approvers.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-4 py-8 text-center text-gray-400 text-sm">No approvers configured</td>
                  </tr>
                ) : approvers.map(a => (
                  <tr key={a.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3">
                      <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700">
                        {a.domain}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-gray-700 text-xs">{a.approver_email || "—"}</td>
                    <td className="px-4 py-3 text-gray-500 text-xs">{a.approver_user_id || "—"}</td>
                    <td className="px-4 py-3 text-gray-400 text-xs">{a.created_by || "—"}</td>
                    <td className="px-4 py-3 text-gray-400 text-xs">{toISTDate(a.created_at) || "—"}</td>
                    <td className="px-4 py-3">
                      <button
                        onClick={() => removeApprover(a.id)}
                        className="px-2 py-1 bg-red-100 text-red-600 text-xs rounded-md hover:bg-red-200"
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Reject modal */}
      {rejectModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md">
            <h2 className="text-base font-semibold mb-3">
              Reject — {rejectModal.name}
            </h2>
            <textarea
              value={rejectReason}
              onChange={e => setRejectReason(e.target.value)}
              placeholder="Reason (required)"
              className="w-full border border-gray-200 rounded-lg p-3 text-sm h-24 resize-none focus:outline-none focus:ring-2 focus:ring-red-300"
            />
            <div className="flex gap-2 mt-4 justify-end">
              <button onClick={() => setRejectModal(null)}
                className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">
                Cancel
              </button>
              <button
                disabled={!rejectReason.trim()}
                onClick={async () => {
                  await action(rejectModal.name, "reject", { reason: rejectReason });
                  setRejectModal(null);
                }}
                className="px-4 py-2 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed">
                Confirm Reject
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
