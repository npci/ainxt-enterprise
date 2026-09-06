// SPDX-License-Identifier: MIT
import { useState, useEffect, useRef } from "react";
import { Database, Plus, Trash2, RefreshCw, Circle, GitBranch, Clock, CheckCircle, XCircle, AlertTriangle, Send, Layers, Activity } from "lucide-react";

import { API_BASE as API, authFetch } from '../config';
import { toIST, toISTDate } from '../utils/time';
import { useToast, useConfirm } from './ui/DialogProvider.jsx';
import {
  validateURL,
  validateRepoName,
  validateDescription,
  getErrorMessage,
} from "../utils/securityValidation";

function StatusDot({ status }) {
  const color =
    status === "ready"       ? "text-green-500" :
    status === "running"     ? "text-yellow-500" :
    status === "pending"     ? "text-blue-400" :
    status === "failed"      ? "text-red-500" :
    status === "rejected"    ? "text-red-300" :
    "text-gray-300";
  return <Circle size={8} className={`fill-current ${color}`} />;
}

function RequestStatusBadge({ status }) {
  const map = {
    pending:  { bg: "bg-yellow-100 text-yellow-700", label: "Pending Approval" },
    approved: { bg: "bg-green-100 text-green-700",   label: "Approved" },
    rejected: { bg: "bg-red-100 text-red-700",       label: "Rejected" },
    running:  { bg: "bg-blue-100 text-blue-700",     label: "Indexing" },
    done:     { bg: "bg-green-100 text-green-700",   label: "Done" },
    failed:   { bg: "bg-red-100 text-red-700",       label: "Failed" },
  };
  const s = map[status] || { bg: "bg-gray-100 text-gray-600", label: status };
  return <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${s.bg}`}>{s.label}</span>;
}

export default function CodebaseManager({ user }) {
  const { toast }   = useToast();
  const { confirm } = useConfirm();
  const [repos, setRepos]         = useState([]);
  // Section-wide infra readiness (index-worker / embed-svc) -- shown
  // regardless of which repo is selected, so problems are visible as soon
  // as the panel opens rather than only after a job gets stuck.
  const [infra, setInfra]         = useState(null);
  const [requests, setRequests]   = useState([]);
  const [selected, setSelected]   = useState(null);
  const [showAdd, setShowAdd]     = useState(false);
  const [activeTab, setActiveTab] = useState("repos"); // repos | requests | health
  const [products, setProducts]   = useState([]);
  const [form, setForm]           = useState({ gitlab_url: "", branch: "main", note: "", product_id: "" });
  const [formErrors, setFormErrors] = useState({ gitlab_url: "", branch: "", note: "", product_id: "" });
  const [submitting, setSubmitting] = useState(false);
  const [submitResult, setSubmitResult] = useState(null);
  const [searchQ, setSearchQ]     = useState("");
  const [requestSearch, setRequestSearch] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [healthData, setHealthData] = useState(null);
  const [bulkLoading, setBulkLoading] = useState(false);
  const [bulkResult, setBulkResult] = useState(null);
  const pollTimerRef = useRef(null);

  const isAdmin  = user?.role === "admin";
  // isC1Plus: senior-level access gate — driven by can_approve from backend
  // (backend computes this using APPROVAL_AD_LEVEL config, not hardcoded 3)
  const isC1Plus = isAdmin || (user?.can_approve === true);

  useEffect(() => {
    loadRepos();
    loadProducts();
    loadRequests();  // all users can see their own submissions
    return () => clearTimeout(pollTimerRef.current);
  }, []);

  // Auto-refresh repos list while any repo is running/pending indexing
  useEffect(() => {
    clearTimeout(pollTimerRef.current);
    if (repos.some(r => ["running", "pending"].includes(r.status))) {
      pollTimerRef.current = setTimeout(() => {
        loadRepos();
        loadRequests();
      }, 3000);
    }
    return () => clearTimeout(pollTimerRef.current);
  }, [repos]);

  async function loadRepos() {
    const r = await authFetch(`${API}/index/repos`);
    const d = await r.json();
    const fresh = d.repos || [];
    setRepos(fresh);
    setInfra(d.infra || null);
    setSelected(prev => {
      if (!prev || prev._type === "request") return prev;
      const updated = fresh.find(r => r.name === prev.name && r.branch === prev.branch);
      return updated || prev;
    });
  }

  async function loadProducts() {
    try {
      const r = await authFetch(`${API}/products`);
      const d = await r.json();
      // Only ACTIVE products can be linked to a codebase request
      setProducts((d.products || []).filter(p => p.status === "ACTIVE"));
    } catch { setProducts([]); }
  }

  async function loadRequests() {
    const r = await authFetch(`${API}/index/requests`);
    const d = await r.json();
    setRequests(d.requests || []);
  }

  // Field validation helper
  function validateField(fieldName, value) {
    let result;
    switch (fieldName) {
      case "gitlab_url":
        result = validateURL(value, { fieldName: "GitLab URL", allowedSchemes: ["https:"] });
        break;
      case "branch":
        // Required field — must not be empty
        if (!value || !value.trim()) {
          return "Branch is required";
        }
        // Allow alphanumeric, hyphens, underscores, periods, forward slashes
        if (!/^[a-zA-Z0-9/_\-.]+$/.test(value.trim())) {
          return "Branch can only contain alphanumeric characters, hyphens, underscores, periods, and forward slashes";
        }
        result = { isValid: true, errors: [] };
        break;
      case "note":
        result = validateDescription(value);
        break;
      case "product_id":
        // Required field — must select a product
        if (!value || !value.trim()) {
          return "Product is required";
        }
        result = { isValid: true, errors: [] };
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
    // Clear error when user starts typing (but only for fields that were previously invalid)
    if (formErrors[fieldName] && formErrors[fieldName] !== "") {
      setFormErrors(prev => ({ ...prev, [fieldName]: "" }));
    }
  }

  async function submitRequest() {
    // Validate all required fields
    const errors = {
      gitlab_url: validateField("gitlab_url", form.gitlab_url),
      branch: validateField("branch", form.branch),
      note: validateField("note", form.note),
      product_id: validateField("product_id", form.product_id),
    };

    // Check if any errors exist
    const hasErrors = Object.values(errors).some(e => e !== "");
    if (hasErrors) {
      setFormErrors(errors);
      return;
    }

    if (!form.gitlab_url.trim()) {
      setSubmitResult({ error: "Enter a GitLab repository URL" });
      return;
    }
    if (!form.gitlab_url.startsWith("https://")) {
      setSubmitResult({ error: "URL must start with https://" });
      return;
    }
    if (!form.branch.trim()) {
      setSubmitResult({ error: "Branch is required" });
      return;
    }
    if (!form.product_id) {
      setSubmitResult({ error: "Product is required" });
      return;
    }
    setSubmitting(true);
    setSubmitResult(null);
    try {
      const r = await authFetch(`${API}/index/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          gitlab_url: form.gitlab_url.trim(),
          branch: form.branch,
          note: form.note,
          product_id: form.product_id || null,
        }),
      });
      // r.json() throws on a non-JSON body (e.g. an unhandled 500's plain-
      // text "Internal Server Error") — previously uncaught, which left
      // setSubmitting(false) never reached: the button stayed stuck on
      // "submitting" forever with no error shown at all.
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        setForm({ gitlab_url: "", branch: "main", note: "", product_id: "" });
        setFormErrors({ gitlab_url: "", branch: "", note: "", product_id: "" });
        setShowAdd(false);
        setSubmitResult(null);
        loadRepos();
        loadRequests();
        setActiveTab("requests");  // switch to requests tab so user sees their submission
      } else {
        setSubmitResult({ error: d.detail || "Submission failed" });
      }
    } catch (err) {
      setSubmitResult({ error: err.message || "Submission failed" });
    } finally {
      setSubmitting(false);
    }
  }

  async function approveRequest(reqId) {
    // r.ok was never checked — a rejected approval (4-eyes, dept gate, "already
    // approved", missing-token, etc.) still cleared the panel and switched to
    // the Repos tab as if it had succeeded, with no error shown at all.
    const r = await authFetch(`${API}/index/requests/${reqId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: reviewNote }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      toast.error(d.detail || "Approve failed");
      return;
    }
    setReviewNote("");
    setSelected(null);
    setActiveTab("repos");   // switch to Repos tab so user sees indexing start
    await loadRepos();
    loadRequests();
  }

  async function rejectRequest(reqId) {
    if (!reviewNote) { toast.warn("Please provide a rejection reason"); return; }
    const r = await authFetch(`${API}/index/requests/${reqId}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: reviewNote }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      toast.error(d.detail || "Reject failed");
      return;
    }
    setReviewNote("");
    loadRequests();
  }

  async function deleteRepo(slug, name) {
    const ok = await confirm({ title: "Delete Codebase", message: `Delete "${selected.name}"? This cannot be undone.`, confirmLabel: "Delete" });
    if (!ok) return;
   try {
     // The backend requires product_id + branch as query params (deletes
     // only THIS product/branch's vectors, since a repo can be indexed under
     // several) -- previously never sent, so every delete 422'd silently.
     const params = new URLSearchParams({
       product_id: selected.product_id || "",
       branch:     selected.branch || "",
     });
     const res =  await authFetch(`${API}/index/repos/${slug}?${params}`, { method: "DELETE" });
     if (!res.ok && res.status !== 204) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Delete failed");
      }
    setRepos(prev => prev.filter(r => r.slug !== slug));
    if (selected?.slug === slug) setSelected(null);
   } catch (error) {
     // Was silently swallowed — a failed delete (e.g. 403 department gate)
     // showed no feedback at all, looking identical to a successful delete.
     toast.error(error.message || "Delete failed");
   }
  }

  async function reindex(slug) {
    // Optimistically marked "running" unconditionally before — a rejected
    // reindex (not admin, missing token, etc.) still showed the repo as
    // running in the panel even though nothing was ever triggered. This is
    // exactly the "repos panel must never show running unless indexing
    // actually started" requirement the backend side of this already
    // enforces (see _mark_index_request_failed) — the UI must not
    // re-introduce that gap on its own optimistic update.
    const r = await authFetch(`${API}/index/repos/${slug}/reindex`, { method: "POST" });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      toast.error(d.detail || "Reindex failed");
      return;
    }
    // Mark as running immediately; auto-poll useEffect will refresh every 3s
    setRepos(prev => prev.map(r => r.slug === slug ? { ...r, status: "running" } : r));
  }

  async function loadHealth() {
    try {
      const r = await authFetch(`${API}/index/health`);
      const d = await r.json();
      setHealthData(d);
    } catch (e) {
      setHealthData({ error: e.message });
    }
  }

  async function bulkReindex(staleOnly) {
    setBulkLoading(true); setBulkResult(null);
    try {
      const r = await authFetch(`${API}/index/bulk`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stale_only: staleOnly, stale_days: 7 }),
      });
      const d = await r.json();
      setBulkResult(d);
      setTimeout(loadHealth, 2000);
    } catch (e) {
      setBulkResult({ error: e.message });
    } finally {
      setBulkLoading(false);
    }
  }

  function fmt(n) { return n ? n.toLocaleString() : "—"; }
  const filtered = repos.filter(r => !searchQ || r.name.toLowerCase().includes(searchQ.toLowerCase()));
  const pendingCount = requests.filter(r => r.status === "pending").length;

  return (
    <div className="flex h-full">
      {/* LEFT SIDEBAR */}
      <div className="w-72 bg-gray-50 border-r border-gray-200 flex flex-col">
        {/* Sidebar header */}
        <div className="p-4 border-b border-gray-200 flex items-center justify-between">
          <span className="text-sm font-semibold  text-indigo-700">Codebases</span>
          <button
            onClick={() => { setShowAdd(true); setSelected(null); }}
            className="flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded text-white cursor-pointer brand-grad hover:opacity-70"
          >
            <Plus size={12} /> Submit Request
          </button>
        </div>
        {/* Tabs */}
        <div className="flex border-b border-gray-200">
          <button
            onClick={() => setActiveTab("repos")}
            className={`flex-1 px-2 py-2.5 text-xs font-medium cursor-pointer ${activeTab === "repos" ? "border-b-2 border-indigo-600 text-indigo-600" : "text-gray-400 hover:text-gray-600"}`}
          >
            Repos
          </button>
          <button
            onClick={() => setActiveTab("requests")}
            className={`flex-1 px-2 py-2.5 text-xs font-medium relative cursor-pointer ${activeTab === "requests" ? "border-b-2 border-indigo-600 text-indigo-600" : "text-gray-400 hover:text-gray-600"}`}
          >
            Request Status
            {pendingCount > 0 && (
              <span className="absolute top-1.5 right-1 w-4 h-4 flex items-center justify-center bg-amber-400 text-white text-[9px] rounded-full font-bold">
                {pendingCount}
              </span>
            )}
          </button>
          {isAdmin && (
            <button
              onClick={() => { setActiveTab("health"); loadHealth(); }}
              className={`flex-1 px-2 py-2.5 text-xs font-medium cursor-pointer ${activeTab === "health" ? "border-b-2 border-indigo-600 text-indigo-600" : "text-gray-400 hover:text-gray-600"}`}
            >
              Health
            </button>
          )}
        </div>

        {activeTab === "repos" && (
          <>
            <div className="px-3 py-2 border-b border-gray-100">
              <input
                value={searchQ}
                onChange={e => setSearchQ(e.target.value)}
                placeholder="Search repos…"
                className="w-full px-2.5 py-1.5 text-xs border border-gray-200 rounded outline-none focus:border-indigo-300 shadow-sm bg-white"
              />
            </div>
            <div className="flex-1 overflow-y-auto py-1">
              {filtered.length === 0 && (
                <div className="p-6 text-center text-gray-400 text-sm">
                  {repos.length === 0 ? "No index requests yet. Submit one above." : "No matches"}
                </div>
              )}
              {filtered.map(repo => (
                <div
                  key={`${repo.name}:${repo.branch}`}
                  onClick={() => { setSelected(repo); setShowAdd(false); }}
                  className={`px-3 py-2.5 m-1 border-b-1 border-b-gray-100 rounded cursor-pointer transition ${selected?.name === repo.name && selected?.branch === repo.branch && !showAdd ? "bg-indigo-50 text-indigo-700 font-semibold border-l-2 border-l-indigo-500" : "hover:bg-gray-100"}`}
                >
                  <div className="flex items-center gap-2">
                    <StatusDot status={repo.status} />
                    <span className={`text-sm font-medium truncate ${selected?.name === repo.name && selected?.branch === repo.branch && !showAdd ? "text-indigo-700" : "text-gray-600"}`}>{repo.name.split("/").pop()}</span>
                    {repo.vector_count > 0 && (
                      <span className="ml-auto text-xs bg-gray-100 text-gray-500 px-1.5 py-0.5 rounded shrink-0">
                        {fmt(repo.vector_count)}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-gray-400 mt-0.5">{repo.product_name} · <span className="capitalize">{repo.status === "not_indexed" ? "not indexed" : repo.status}</span></div>
                  <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                    {repo.branch && (
                      <span className="inline-flex items-center gap-0.5 bg-blue-50 text-blue-600 text-[10px] px-1.5 py-0.5 rounded">
                        <GitBranch size={9} />
                        {repo.branch}
                      </span>
                    )}
                    {repo.indexed_at && (
                      <span className="text-[10px] text-gray-400">Indexed {toISTDate(new Date(repo.indexed_at * 1000))}</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}

        {activeTab === "health" && isAdmin && (
          <div className="flex-1 overflow-y-auto p-3">
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-semibold text-gray-700">Index Health</span>
              <div className="flex gap-1.5">
                <button
                  onClick={loadHealth}
                  className="p-1 hover:bg-indigo-50 rounded-md text-indigo-700 hover:text-indigo-600 cursor-pointer"
                  title="Refresh"
                >
                  <RefreshCw size={12} />
                </button>
              </div>
            </div>
            {healthData && !healthData.error && (
              <div className="mb-3 flex gap-2 text-xs">
                <span className="bg-gray-100 text-gray-600 px-2 py-1 rounded">
                  {healthData.total} repos
                </span>
                {healthData.stale_count > 0 && (
                  <span className="bg-amber-100 text-amber-600 px-2 py-1 rounded">
                    {healthData.stale_count} stale
                  </span>
                )}
              </div>
            )}
            {bulkResult && (
              <div className={`mb-2 p-2 rounded text-xs ${bulkResult.error ? "bg-red-50 text-red-600" : "bg-green-50 text-green-700"}`}>
                {bulkResult.error
                  ? `Error: ${bulkResult.error}`
                  : `Re-indexing ${bulkResult.triggered} repos (${bulkResult.skipped} skipped)`
                }
              </div>
            )}
            <div className="flex gap-1.5 mb-3">
             
              <button
                onClick={() => bulkReindex(false)}
                disabled={bulkLoading}
                className="flex-1 text-xs py-1.5 text-white rounded brand-grad hover:opacity-80 disabled:opacity-80 cursor-pointer"
              >
                Re-index All
              </button>
               <button
                onClick={() => bulkReindex(true)}
                disabled={bulkLoading}
                className="flex-1 text-xs py-1.5 bg-white border border-gray-300 text-gray-600 rounded hover:bg-gray-100 disabled:opacity-90 cursor-pointer"
              >
                {bulkLoading ? "Queuing…" : "Re-index Stale"}
              </button>
            </div>
            {!healthData && <p className="text-xs text-gray-400 text-center mt-4">Click refresh to load health data</p>}
            {healthData?.error && <p className="text-xs text-red-500">{healthData.error}</p>}
            {healthData?.repos?.map(r => (
              <div key={r.name} className={`mb-1 px-2 py-1.5 rounded text-xs border ${r.is_stale ? "border-amber-200 bg-amber-50" : "border-gray-100 bg-white"}`}>
                <div className="flex items-center justify-between">
                  <span className="font-medium text-gray-600 truncate">{r.name}</span>
                  <span className={`shrink-0 ml-1 px-1.5 py-0.5 rounded text-[10px] font-medium ${
                    r.status === "ready" ? "bg-green-100 text-green-700" :
                    r.status === "running" ? "bg-blue-100 text-blue-700" :
                    r.status === "failed" ? "bg-red-100 text-red-700" :
                    "bg-gray-100 text-gray-500"
                  }`}>{r.status}</span>
                </div>
                <div className="flex items-center gap-2 mt-0.5 text-gray-400">
                  <span>{r.vector_count?.toLocaleString() || 0} chunks</span>
                  {r.days_since != null && <span>· {r.days_since}d ago {r.is_stale ? "⚠️" : ""}</span>}
                </div>
              </div>
            ))}
          </div>
        )}

        {activeTab === "requests" && (
          <>
            <div className="p-2 border-b border-gray-100">
              <input
                value={requestSearch}
                onChange={e => setRequestSearch(e.target.value)}
                placeholder="Search requests..."
                className="w-full px-2.5 py-1.5 text-xs border border-gray-200 rounded outline-none focus:border-indigo-300 shadow-sm bg-white"
              />
            </div>
            <div className="flex-1 overflow-y-auto">
              {requests.filter(r => !requestSearch || r.repo_name.toLowerCase().includes(requestSearch.toLowerCase()) || (r.requested_by || "").toLowerCase().includes(requestSearch.toLowerCase())).length === 0 && (
                <div className="p-6 text-center text-gray-400 text-sm">{requests.length === 0 ? "No index requests yet" : "No matches"}</div>
              )}
              {requests.filter(r => !requestSearch || r.repo_name.toLowerCase().includes(requestSearch.toLowerCase()) || (r.requested_by || "").toLowerCase().includes(requestSearch.toLowerCase())).map(req => (
                <div
                  key={req.id}
                  className={`px-3 py-2.5 m-1 rounded cursor-pointer transition border-b-1 border-b-gray-100 ${selected?._type === "request" && selected?.id === req.id ? "bg-indigo-50 text-indigo-700 font-semibold border-l-2 border-l-indigo-500" : "text-gray-600 hover:bg-gray-100"}`}
                  onClick={() => setSelected({ _type: "request", ...req })}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <GitBranch size={12} className="text-gray-400" />
                    <span className="text-sm font-medium  truncate">{req.repo_name}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="min-w-fit"><RequestStatusBadge status={req.status} /></span>
                    <span className="text-xs text-gray-400 break-all truncate">{req.branch}</span>
                  </div>
                  {req.requested_by && (
                    <div className="text-xs text-gray-400 mt-0.5">by <span className="font-medium">{req.requested_by}</span></div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {/* MAIN PANEL */}
      <div className="flex-1 overflow-y-auto p-6 bg-white">
        {/* Section-wide infra readiness — shown regardless of which repo (if
            any) is selected, so problems are visible as soon as the panel
            opens rather than only after submitting/watching a stuck job. */}
        {infra && !infra.index_worker_running && (
          <div className="mb-3 p-3 bg-amber-50 border border-amber-200 rounded text-sm text-amber-700">
            <strong>Heads up:</strong> <code>index-worker</code> is not running — indexing
            requests will stay queued indefinitely and never complete.
          </div>
        )}
        {infra && !infra.embed_svc_reachable && (
          <div className="mb-3 p-3 bg-amber-50 border border-amber-200 rounded text-sm text-amber-700">
            <strong>Heads up:</strong> Embedding service is not reachable — indexing will
            fail once it reaches the embedding step, and Workspace/Codebase search quality
            will be degraded. Run <code>docker compose --profile embed up -d embed-svc</code>{" "}
            in the project directory.
          </div>
        )}
        {showAdd ? (
          <div className="max-w-2xl mx-auto">
            <h2 className="font-semibold text-gray-800 mb-1">Submit Index Request</h2>
            <p className="text-xs text-gray-400 mb-4">
              Provide a GitLab HTTPS URL. A C1+ approver will review and trigger cloning using the platform admin token.
            </p>

            {submitResult?.success && (
              <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded text-sm text-green-700">
                <strong>Request submitted!</strong> ID: {submitResult.data.request_id}
                {submitResult.data.is_protected_branch && (
                  <div className="mt-1 flex items-center gap-1 text-yellow-600">
                    <AlertTriangle size={12} />
                    <span>This is a <strong>protected branch</strong> — approver will be notified.</span>
                  </div>
                )}
              </div>
            )}
            {submitResult?.error && (
              <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-sm text-red-600">
                {submitResult.error}
              </div>
            )}

            <div className="space-y-3">
              {/* GitLab URL — user types the actual repo URL */}
              <div>
                <label className="text-xs text-gray-500 font-medium">GitLab Repository URL <span className="text-red-500">*</span></label>
                <input
                  value={form.gitlab_url}
                  onChange={e => handleChange("gitlab_url", e.target.value)}
                  onBlur={() => handleBlur("gitlab_url")}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none focus:border-indigo-300 font-mono ${formErrors.gitlab_url ? "border-red-500" : "border-gray-200"}`}
                  placeholder="https://gitlab.example.com/team/repo-name"
                />
                {formErrors.gitlab_url && (
                  <p className="mt-1 text-xs text-red-600">{formErrors.gitlab_url}</p>
                )}
              </div>

              <div>
                <label className="text-xs text-gray-500 font-medium">Branch <span className="text-red-500">*</span></label>
                <input
                  value={form.branch}
                  onChange={e => handleChange("branch", e.target.value)}
                  onBlur={() => handleBlur("branch")}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none focus:border-indigo-300 ${formErrors.branch ? "border-red-500" : "border-gray-200"}`}
                  placeholder="e.g. main, develop, feature/my-branch"
                />
                {formErrors.branch && (
                  <p className="mt-1 text-xs text-red-600">{formErrors.branch}</p>
                )}
              </div>

              {/* Product — required so vectors are scoped correctly per product+branch */}
              <div>
                <label className="text-xs text-gray-500 font-medium">Product <span className="text-red-500">*</span></label>
                <select
                  value={form.product_id}
                  onChange={e => handleChange("product_id", e.target.value)}
                  onBlur={() => handleBlur("product_id")}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none focus:border-indigo-300 ${formErrors.product_id ? "border-red-500" : "border-gray-200"}`}
                >
                  <option value="">— Select a product —</option>
                  {products.map(p => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
                {formErrors.product_id && (
                  <p className="mt-1 text-xs text-red-600">{formErrors.product_id}</p>
                )}
              </div>

              <div>
                <label className="text-xs text-gray-500 font-medium">Note (optional)</label>
                <textarea
                  value={form.note}
                  onChange={e => handleChange("note", e.target.value)}
                  onBlur={() => handleBlur("note")}
                  rows={2}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none focus:border-indigo-300 resize-none ${formErrors.note ? "border-red-500" : "border-gray-200"}`}
                  placeholder="Why do you need this repo indexed?"
                />
                {formErrors.note && (
                  <p className="mt-1 text-xs text-red-600">{formErrors.note}</p>
                )}
              </div>

              <div className="flex gap-2 pt-1">
                <button
                  onClick={submitRequest}
                  disabled={submitting || !form.gitlab_url.trim() || !form.branch.trim() || !form.product_id}
                  className="flex items-center gap-1.5 px-4 py-2 text-white rounded text-sm brand-grad hover:opacity-70 disabled:opacity-50 cursor-pointer"
                >
                  <Send size={13} />
                  {submitting ? "Submitting…" : "Submit Index Request"}
                </button>
                <button
                  onClick={() => {
                    setShowAdd(false);
                    // Defer cleanup so the form hides instantly
                    requestAnimationFrame(() => {
                      setSubmitResult(null);
                      setForm({ gitlab_url: "", branch: "main", note: "", product_id: "" });
                      setFormErrors({ gitlab_url: "", branch: "", note: "", product_id: "" });
                    });
                  }}
                  className="px-4 py-2 border border-gray-200 rounded text-sm hover:bg-gray-100 cursor-pointer"
                >
                  Cancel
                </button>
              </div>
            </div>
          </div>

        ) : selected?._type === "request" ? (
          <div className="max-w-2xl mx-auto">
            <div className="flex items-start justify-between mb-4">
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <GitBranch size={18} className="text-gray-600" />
                  <h2 className="text-lg font-semibold text-gray-800">{selected.repo_name}</h2>
                  <RequestStatusBadge status={selected.status} />
                </div>
                <div className="flex items-center gap-2 flex-wrap mt-1">
                  {selected.branch && (
                    <span className="inline-flex items-center gap-1 bg-blue-50 text-blue-600 text-xs px-2 py-0.5 rounded">
                      <GitBranch size={11} />
                      {selected.branch}
                    </span>
                  )}
                  <span className="text-xs text-gray-400">Requested by: <strong>{selected.requested_by}</strong></span>
                </div>
              </div>
            </div>

            <div className="space-y-2 text-sm text-gray-600 mb-4 p-4 bg-gray-50 rounded">
              {selected.review_note && (
                <div><span className="font-medium">Note:</span> {selected.review_note}</div>
              )}
              {selected.reviewed_by && (
                <div><span className="font-medium">Reviewed by:</span> {selected.reviewed_by}</div>
              )}
              {selected.error_msg && (
                <div className="text-red-600"><span className="font-medium">Error:</span> {selected.error_msg}</div>
              )}
              <div><span className="font-medium">Created:</span> {toIST(new Date(selected.created_at))}</div>
            </div>

            {isC1Plus && selected.status === "pending" && (
              <div className="border border-yellow-200 bg-yellow-50 rounded p-4">
                <p className="text-sm text-yellow-700">This request is awaiting approval. Use your <strong>Inbox</strong> to approve or reject it.</p>
              </div>
            )}
          </div>

        ) : selected ? (
          <div className="max-w-2xl mx-auto">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-3 flex-wrap">
                <Database size={20} className="text-gray-600" />
                <h2 className="text-lg font-semibold text-gray-800">{selected.name}</h2>
                <StatusDot status={selected.status} />
                <span className="text-sm capitalize text-gray-500">{selected.status}</span>
                {selected.branch && (
                  <span className="inline-flex items-center gap-1 bg-blue-50 text-blue-600 text-xs px-2 py-0.5 rounded">
                    <GitBranch size={11} />
                    {selected.branch}
                  </span>
                )}
              </div>
              {isC1Plus && (
                <div className="flex gap-2">
                  <button
                    onClick={() => reindex(selected.slug)}
                    disabled={["running", "pending"].includes(selected.status)}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded text-sm  disabled:opacity-90 brand-grad hover:opacity-90 cursor-pointer text-white"
                  >
                    <RefreshCw size={13} className={selected.status === "running" ? "animate-spin" : ""} />
                    Re-index
                  </button>
                  <button
                    onClick={() => deleteRepo(selected.slug, selected.name)}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-red-500 rounded text-sm hover:bg-red-50 cursor-pointer"
                  >
                    <Trash2 size={13} /> Delete
                  </button>
                </div>
              )}
            </div>

            <div className="grid grid-cols-4 gap-4 p-4 bg-gray-50 rounded-lg mb-4">
              <div className="text-center">
                <div className="text-xl font-semibold text-gray-800">{fmt(selected.vector_count)}</div>
                <div className="text-xs text-gray-400">Vectors</div>
              </div>
              <div className="text-center">
                <div className="text-sm font-semibold text-gray-800 capitalize">{selected.status}</div>
                <div className="text-xs text-gray-400">Status</div>
              </div>
              <div className="text-center">
                <div className="flex items-center justify-center gap-1 font-semibold text-gray-800">
                  <GitBranch size={13} className="text-blue-500" />
                  <span className="text-sm">{selected.branch || "—"}</span>
                </div>
                <div className="text-xs text-gray-400 mt-0.5">Branch</div>
              </div>
              <div className="text-center">
                <div className="text-sm font-semibold text-gray-800">
                  {selected.indexed_at ? toISTDate(new Date(selected.indexed_at * 1000)) : "—"}
                </div>
                <div className="text-xs text-gray-400">Indexed</div>
              </div>
            </div>

            {selected.url && (
              <div className="mb-3 p-3 bg-gray-50 rounded text-xs text-gray-500 break-all">
                <span className="font-medium">GitLab:</span> {selected.url}
              </div>
            )}

            <div className="p-3 bg-indigo-50 rounded text-sm text-indigo-700">
              <strong>Collection:</strong> repo_{selected.name} · Use this repo name in Projects to scope Q&A.
            </div>

            {selected.status === "running" && (
              <div className="mt-3 flex items-center gap-2 text-sm text-amber-400">
                <RefreshCw size={14} className="animate-spin" />
                Indexing in progress…
              </div>
            )}
            {selected.status === "running" && selected.warning && (
              <div className="mt-2 p-3 bg-amber-50 border border-amber-200 rounded text-sm text-amber-700">
                <strong>Heads up:</strong> {selected.warning}
              </div>
            )}
            {selected.status === "pending" && (
              <div className="mt-3 flex items-center gap-2 text-sm text-indigo-700">
                <RefreshCw size={14} />
                Awaiting approval before indexing starts.
              </div>
            )}
            {selected.status === "failed" && (
              <div className="mt-3 p-3 bg-red-50 border border-red-200 rounded text-sm text-red-700 space-y-2">
                <div><strong>Indexing failed.</strong> {selected.error || "Check server logs."}</div>
                <button
                  onClick={async () => {
                    await deleteRepo(selected.slug, selected.name);
                    setShowAdd(true);
                  }}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-red-600 text-white rounded text-sm hover:bg-red-700 cursor-pointer"
                >
                  <Trash2 size={12} /> Delete & Re-submit
                </button>
              </div>
            )}
          </div>

        ) : (
          <div className="flex flex-col items-center justify-center h-full text-gray-300">
            <Database size={48} />
            <p className="mt-3 text-sm">Select a repo or submit a new index request</p>
          </div>
        )}
      </div>
    </div>
  );
}
