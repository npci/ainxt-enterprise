// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from "react";
import {
  Mail,
  Users,
  Send,
  Paperclip,
  Sparkles,
  Eye,
  Trash2,
  RefreshCw,
  X,
  ChevronDown,
  Check,
  Search,
  Clock,
  CheckCircle2,
  XCircle,
  AlertCircle,
} from "lucide-react";
import { API_BASE, authFetch, apiFetch } from "../config";
import { useToast } from "./ui/DialogProvider";
import { validateBroadcastSubject, validateBroadcastHtmlBody, validateFreeText } from "../utils/securityValidation";
import { decryptPii } from "../utils/piiCrypto";

// ── Styling constants ────────────────────────────────────────────────────────
// Input/textarea: matches Profile.jsx focus style (focus:border-indigo-300, no ring)
const INPUT_CLS =
  "w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 " +
  "focus:outline-none focus:border-indigo-300";
const GRADIENT_BTN =
  "px-4 py-2 text-white text-sm rounded-lg brand-grad " +
  "hover:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer inline-flex items-center gap-2";

const STATUS_BADGE = {
  draft:     "bg-gray-100 text-gray-700",
  queued:    "bg-blue-50 text-blue-700",
  sending:   "bg-indigo-100 text-indigo-700",
  completed: "bg-green-100 text-green-700",
  failed:    "bg-red-100 text-red-700",
  cancelled: "bg-yellow-100 text-yellow-700",
};

const STATUS_ICON = {
  draft:     <Clock size={11} />,
  queued:    <Clock size={11} />,
  sending:   <RefreshCw size={11} className="animate-spin" />,
  completed: <CheckCircle2 size={11} />,
  failed:    <XCircle size={11} />,
  cancelled: <AlertCircle size={11} />,
};

const BADGE_BASE =
  "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium";

const fmtBytes = (n) => {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
};

const fmtDate = (s) => {
  if (!s) return "—";
  try { return new Date(s).toLocaleString(); } catch { return s; }
};

const fmtDateShort = (s) => {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch { return s; }
};

// ── Preview Modal ─────────────────────────────────────────────────────────────
function PreviewModal({ htmlBody, previewName, previewEnrich, modelUsed, subject,
                        onClose, onNameChange, onEnrichChange }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "radial-gradient(ellipse at 50% 40%, rgba(99,102,241,0.12) 0%, rgba(0,0,0,0.55) 100%)" }}
      onClick={onClose}
    >
      <div
        className="flex flex-col overflow-hidden w-full"
        style={{ maxWidth: "1100px", height: "94vh", background: "#0f0f13", borderRadius: "20px", boxShadow: "0 32px 80px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,255,255,0.07), inset 0 1px 0 rgba(255,255,255,0.08)" }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* ── Header ── */}
        <div
          className="flex-shrink-0 flex items-center gap-3 px-5 py-3"
          style={{
            background: "linear-gradient(135deg, #1e1b4b 0%, #312e81 50%, #1e1b4b 100%)",
            borderBottom: "1px solid rgba(99,102,241,0.3)",
            boxShadow: "0 1px 0 rgba(255,255,255,0.04), inset 0 1px 0 rgba(255,255,255,0.06)",
          }}
        >
          {/* Icon + title */}
          <div className="flex items-center gap-2.5 flex-shrink-0">
            <div
              className="w-8 h-8 rounded-xl flex items-center justify-center flex-shrink-0"
              style={{ background: "rgba(99,102,241,0.35)", border: "1px solid rgba(139,92,246,0.5)", boxShadow: "0 0 12px rgba(99,102,241,0.4)" }}
            >
              <Eye size={14} className="text-indigo-200" />
            </div>
            <div className="flex-shrink-0">
              <p className="text-xs font-bold text-white leading-none tracking-wide">Email Preview</p>
              <p className="text-xs text-indigo-400 mt-0.5 leading-none">Live render</p>
            </div>
          </div>

          {/* Subject pill */}
          {subject && (
            <>
              <div className="w-px h-5 flex-shrink-0" style={{ background: "rgba(99,102,241,0.4)" }} />
              <div
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg truncate min-w-0 flex-1 max-w-sm"
                style={{ background: "rgba(99,102,241,0.18)", border: "1px solid rgba(139,92,246,0.3)" }}
                title={subject}
              >
                <Mail size={10} className="text-indigo-300 flex-shrink-0" />
                <span className="text-xs text-indigo-200 truncate font-medium">{subject}</span>
              </div>
            </>
          )}

          {/* Spacer */}
          <div className="flex-1" />

          {/* Personalise toggle */}
          <div
            className="flex items-center gap-2.5 px-3 py-1.5 rounded-lg flex-shrink-0"
            style={{ background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.1)" }}
          >
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <div className="relative flex-shrink-0">
                <input
                  type="checkbox"
                  checked={previewEnrich}
                  onChange={(e) => onEnrichChange(e.target.checked)}
                  className="sr-only"
                />
                <div
                  className="w-7 h-4 rounded-full transition-all duration-200 flex-shrink-0"
                  style={{ background: previewEnrich ? "linear-gradient(135deg,#6366f1,#8b5cf6)" : "rgba(255,255,255,0.15)" }}
                >
                  <div
                    className="w-3 h-3 bg-white rounded-full shadow-sm absolute top-0.5 transition-all duration-200"
                    style={{ left: previewEnrich ? "14px" : "2px" }}
                  />
                </div>
              </div>
              <span className="text-xs font-medium text-indigo-200 whitespace-nowrap">Personalise</span>
            </label>
          </div>

          {/* Name input */}
          <input
            type="text"
            value={previewName}
            onChange={(e) => onNameChange(e.target.value)}
            disabled={!previewEnrich}
            placeholder="Recipient name"
            className="rounded-lg px-3 py-1.5 text-xs focus:outline-none transition-all w-32 flex-shrink-0"
            style={previewEnrich
              ? { background: "rgba(255,255,255,0.1)", border: "1px solid rgba(139,92,246,0.5)", color: "#e0e7ff", caretColor: "#a5b4fc" }
              : { background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", color: "rgba(165,180,252,0.4)", cursor: "not-allowed" }}
          />

          {/* Model badge */}
          {modelUsed && (
            <div
              className="hidden sm:flex items-center gap-1.5 px-2.5 py-1 rounded-lg flex-shrink-0"
              style={{ background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.1)" }}
            >
              <Sparkles size={10} className="text-violet-400" />
              <span className="text-xs font-mono text-indigo-300">{modelUsed}</span>
            </div>
          )}

          {/* Close */}
          <button
            onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-xl transition-all duration-150 flex-shrink-0"
            style={{ background: "rgba(255,255,255,0.07)", border: "1px solid rgba(255,255,255,0.12)", cursor: "pointer" }}
            onMouseEnter={e => e.currentTarget.style.background = "rgba(239,68,68,0.25)"}
            onMouseLeave={e => e.currentTarget.style.background = "rgba(255,255,255,0.07)"}
          >
            <X size={13} className="text-indigo-300" />
          </button>
        </div>

        {/* ── Email canvas — full width, no wrapper ── */}
        <div className="flex-1 min-h-0 overflow-hidden">
          <iframe
            title="email-preview"
            srcDoc={htmlBody}
            sandbox=""
            className="w-full h-full bg-white"
          />
        </div>
      </div>
    </div>
  );
}


// ── Broadcast Confirm Modal ───────────────────────────────────────────────────
function BroadcastConfirmModal({ subject, resolveCount, enrichName, attachments, onConfirm, onCancel }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onCancel(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const rows = [
    { icon: Mail,      label: "Subject",     value: subject },
    { icon: Users,     label: "Recipients",  value: `${resolveCount} recipient${resolveCount === 1 ? "" : "s"}` },
    { icon: Sparkles,  label: "Personalise", value: enrichName ? "On — {{name}} replaced per recipient" : "Off" },
    { icon: Paperclip, label: "Attachments", value: attachments > 0 ? `${attachments} file${attachments === 1 ? "" : "s"}` : "None" },
  ];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.55)", backdropFilter: "blur(4px)" }}
      onClick={onCancel}
    >
      <div
        className="w-full overflow-hidden"
        style={{ maxWidth: 460, borderRadius: 20, background: "#fff", boxShadow: "0 24px 64px rgba(0,0,0,0.18), 0 0 0 1px rgba(99,102,241,0.12)" }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-2 px-5 py-4 border-b border-gray-100">
          <div className="flex items-center gap-2">
            <div className="w-5 h-5 rounded-md brand-grad-vivid flex items-center justify-center flex-shrink-0 shadow-sm">
              <Send size={11} className="text-white" />
            </div>
            <span className="text-sm font-semibold text-gray-800">Send Broadcast</span>
            <span className="text-xs text-gray-400 ml-1">— Review before sending</span>
          </div>
          <button onClick={onCancel} className="w-7 h-7 flex items-center justify-center rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-all cursor-pointer">
            <X size={14} />
          </button>
        </div>

        {/* Summary rows */}
        <div className="px-6 py-4 space-y-2">
          {rows.map(({ icon: Icon, label, value }) => (
            <div key={label} className="flex items-center gap-3 px-4 py-2.5 rounded-xl bg-gray-50 border border-gray-100">
              <div className="w-7 h-7 rounded-lg brand-grad-vivid flex items-center justify-center flex-shrink-0 shadow-sm">
                <Icon size={12} className="text-white" />
              </div>
              <span className="text-xs font-semibold text-gray-400 w-20 flex-shrink-0">{label}</span>
              <span className="text-xs text-gray-800 font-medium truncate flex-1" title={value}>{value}</span>
            </div>
          ))}
          <div className="flex items-start gap-2.5 px-4 py-2.5 rounded-xl bg-amber-50 border border-amber-100 mt-1">
            <AlertCircle size={13} className="text-amber-500 flex-shrink-0 mt-0.5" />
            <p className="text-xs text-amber-700 leading-relaxed">
              One email will be queued per recipient. Sending cannot be partially undone, but you can cancel the broadcast mid-flight.
            </p>
          </div>
        </div>

        {/* Footer */}
        <div className="px-6 py-4 flex items-center justify-end gap-2.5 border-t border-gray-100 bg-gray-50/60">
          <button onClick={onCancel} className="px-4 py-2 text-sm font-medium text-gray-600 bg-white border border-gray-200 rounded-xl hover:bg-gray-50 transition-colors cursor-pointer">
            Cancel
          </button>
          <button onClick={onConfirm} className="inline-flex items-center gap-2 px-5 py-2 text-sm font-semibold text-white rounded-xl brand-grad hover:opacity-90 shadow-md transition-all cursor-pointer">
            <Send size={13} /> Send broadcast
          </button>
        </div>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────────────
// Main component
// ──────────────────────────────────────────────────────────────────────────────
export default function EmailBroadcast({ user }) {
  const { toast }   = useToast();
  // PII payload encryption flag (core/pii_crypto.py) — fetched once from the
  // unauthenticated /auth/ui-config endpoint; used to decrypt "pii:v1:"
  // email/name fields returned by /auth/users and /broadcast/* endpoints.
  const piiEnabledPromise = useRef(
    apiFetch(`${API_BASE}/auth/ui-config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => !!d?.pii_payload_encryption_enabled)
      .catch(() => false)
  );
  const [accessAllowed, setAccessAllowed] = useState(null);
  const [history,        setHistory]       = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historySearch,  setHistorySearch]  = useState("");
  const [historyPage,    setHistoryPage]    = useState(0);
  const HISTORY_PAGE_SIZE = 5;
  const [detailId,       setDetailId]      = useState(null);
  const [confirmOpen,    setConfirmOpen]   = useState(false);

  // ── Compose ──────────────────────────────────────────────────────────────
  const [intent,      setIntent]      = useState("");
  const [intentError, setIntentError] = useState("");
  const [tone,        setTone]        = useState("professional");
  const [htmlBody,    setHtmlBody]    = useState("");
  const [modelUsed,   setModelUsed]   = useState("");
  const [generating,  setGenerating]  = useState(false);

  // Preview modal
  const [previewOpen,   setPreviewOpen]   = useState(false);
  const [previewName,   setPreviewName]   = useState("Priyadharshan");
  const [previewEnrich, setPreviewEnrich] = useState(true);
  const [previewHtml,   setPreviewHtml]   = useState("");

  // ── Targeting ────────────────────────────────────────────────────────────
  const [departments,       setDepartments]       = useState([]);
  const [departmentsLoaded, setDepartmentsLoaded] = useState(false);
  const [targetAll,         setTargetAll]         = useState(false);
  const [selectedDepts,     setSelectedDepts]     = useState([]);
  const [maxAdLevel,        setMaxAdLevel]        = useState("");
  const [individuals,       setIndividuals]       = useState([]);
  const [userSearch,        setUserSearch]        = useState("");
  const [userResults,       setUserResults]       = useState([]);
  const [userSearchLoading, setUserSearchLoading] = useState(false);
  const [resolveCount,      setResolveCount]      = useState(null);
  const [resolveSample,     setResolveSample]     = useState([]);
  const [resolving,         setResolving]         = useState(false);
  const [deptDropdownOpen,  setDeptDropdownOpen]  = useState(false);
  const [deptSearch,        setDeptSearch]        = useState("");
  const deptDropdownRef  = useRef(null);
  const userSearchRef    = useRef(null);

  // ── Options ──────────────────────────────────────────────────────────────
  const [subject,     setSubject]     = useState("");
  const [textBody,    setTextBody]    = useState("");
  const [enrichName,  setEnrichName]  = useState(true);
  const [attachments,  setAttachments]  = useState([]);
  const [uploading,    setUploading]    = useState(false);
  const [uploadError,  setUploadError]  = useState(null);
  const fileInputRef = useRef(null);

  const [sending, setSending] = useState(false);

  // ── Lifecycle ────────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    authFetch(`${API_BASE}/broadcast/access`)
      .then((r) => (r.ok ? r.json() : { allowed: false }))
      .then((d) => {
        if (cancelled) return;
        const allowed = !!d.allowed;
        setAccessAllowed(allowed);
        if (allowed) { loadHistory(); loadDepartments(); }
      })
      .catch(() => !cancelled && setAccessAllowed(false));
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live preview enrichment
  useEffect(() => {
    if (!htmlBody) { setPreviewHtml(""); return; }
    if (!previewEnrich) { setPreviewHtml(htmlBody); return; }
    const ctrl = new AbortController();
    authFetch(`${API_BASE}/broadcast/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ html: htmlBody, sample_name: previewName || "Priyadharshan", enrich_name: previewEnrich }),
      signal: ctrl.signal,
    })
      .then((r) => {
        if (!r.ok) throw new Error("not ok");
        return r.json();
      })
      .then((d) => setPreviewHtml(d?.html || htmlBody))
      .catch(() => {});
    return () => ctrl.abort();
  }, [htmlBody, previewEnrich, previewName]);

  // User search debounce
  useEffect(() => {
    if (!userSearch || userSearch.length < 2) { setUserResults([]); return; }
    const t = setTimeout(() => {
      setUserSearchLoading(true);
      authFetch(`${API_BASE}/auth/users?search=${encodeURIComponent(userSearch)}&page_size=15`)
        .then((r) => (r.ok ? r.json() : { users: [] }))
        .then(async (d) => {
          const piiOn = await piiEnabledPromise.current;
          const decrypted = await Promise.all((d.users || []).map(async (u) => ({
            ...u,
            email: await decryptPii(u.email, piiOn),
            name:  await decryptPii(u.name,  piiOn),
          })));
          setUserResults(decrypted);
        })
        .catch(() => setUserResults([]))
        .finally(() => setUserSearchLoading(false));
    }, 250);
    return () => clearTimeout(t);
  }, [userSearch]);

  // Close dept dropdown on outside click
  useEffect(() => {
    if (!deptDropdownOpen) return;
    const onClick = (e) => {
      if (deptDropdownRef.current && !deptDropdownRef.current.contains(e.target))
        setDeptDropdownOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [deptDropdownOpen]);

  // Close individual search results on outside click
  useEffect(() => {
    if (!userResults.length) return;
    const onClick = (e) => {
      if (userSearchRef.current && !userSearchRef.current.contains(e.target))
        setUserResults([]);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [userResults.length]);

  // Poll history while detail open
  useEffect(() => {
    if (!detailId) return;
    let cancelled = false;
    const id = setInterval(() => { if (!cancelled) loadHistory(); }, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [detailId]);

  // ── Data fetchers ────────────────────────────────────────────────────────
  function loadHistory() {
    setHistoryLoading(true);
    authFetch(`${API_BASE}/broadcast?limit=50`)
      .then((r) => (r.ok ? r.json() : { items: [] }))
      .then((d) => setHistory(d.items || []))
      .catch(() => {})
      .finally(() => setHistoryLoading(false));
  }

  function loadDepartments() {
    authFetch(`${API_BASE}/broadcast/departments`)
      .then((r) => (r.ok ? r.json() : { departments: [] }))
      .then((d) => { setDepartments(d.departments || []); setDepartmentsLoaded(true); })
      .catch(() => setDepartmentsLoaded(true));
  }

  // ── Actions ──────────────────────────────────────────────────────────────
  async function handleGenerate() {
    if (!intent.trim()) { setIntentError("Please describe what the email should say."); return; }
    setIntentError("");
    setGenerating(true);
    try {
      const r = await authFetch(`${API_BASE}/broadcast/templates/suggest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ intent, tone }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        const detail = err.detail;
        const text = typeof detail === "string" ? detail
          : detail?.error === "compliance_blocked"
            ? `Compliance blocked the input (${(detail.blocked_types || []).join(", ") || "policy"}).`
            : "Template generation failed.";
        throw new Error(text);
      }
      const d = await r.json();
      setHtmlBody(d.html || "");
      setModelUsed(d.model || "");
      toast.success("Template generated");
    } catch (e) {
      toast.error(e.message || "Template generation failed.");
    } finally {
      setGenerating(false);
    }
  }

  function addIndividual(u) {
    if (!u || !u.email) return;
    if (individuals.some((x) => x.email.toLowerCase() === u.email.toLowerCase())) return;
    setIndividuals((prev) => [...prev, { id: u.id, email: u.email, name: u.name }]);
    setUserSearch(""); setUserResults([]);
  }

  function removeIndividual(email) {
    setIndividuals((prev) => prev.filter((u) => u.email !== email));
  }

  function toggleDept(d) {
    setSelectedDepts((prev) => prev.includes(d) ? prev.filter((x) => x !== d) : [...prev, d]);
  }

  function buildTargeting() {
    return {
      all:          targetAll,
      departments:  selectedDepts,
      max_ad_level: maxAdLevel === "" ? null : Number(maxAdLevel),
      user_ids:     individuals.filter((u) => u.id).map((u) => u.id),
      emails:       individuals.filter((u) => !u.id).map((u) => u.email),
    };
  }

  async function handleResolve() {
    setResolving(true); setResolveCount(null); setResolveSample([]);
    try {
      const r = await authFetch(`${API_BASE}/broadcast/recipients/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildTargeting()),
      });
      if (!r.ok) throw new Error("Audience resolution failed");
      const d = await r.json();
      const piiOn = await piiEnabledPromise.current;
      const decrypted = await Promise.all((d.sample || []).map(async (s) => ({
        ...s,
        email: await decryptPii(s.email, piiOn),
        name:  await decryptPii(s.name,  piiOn),
      })));
      setResolveCount(d.count || 0);
      setResolveSample(decrypted);
    } catch (e) {
      toast.error(e.message || "Audience resolution failed.");
    } finally {
      setResolving(false);
    }
  }

  async function handleUpload(files) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setUploadError(null);
    try {
      for (const f of files) {
        const form = new FormData();
        form.append("file", f);
        const r = await authFetch(`${API_BASE}/broadcast/attachments`, { method: "POST", body: form });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error(err.detail || `Could not upload ${f.name}`);
        }
        const d = await r.json();
        setAttachments((prev) => [...prev, d]);
      }
      toast.success("Attachment uploaded");
    } catch (e) {
      setUploadError(String(e.message || e));
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function handleRemoveAttachment(id) {
    const r = await authFetch(`${API_BASE}/broadcast/attachments/${id}`, { method: "DELETE" });
    if (r.ok) {
      setAttachments((prev) => prev.filter((a) => a.id !== id));
    } else {
      const err = await r.json().catch(() => ({}));
      setUploadError(err.detail || "Could not remove attachment");
    }
  }

  async function handleSend() {
    if (!subject.trim()) { toast.error("Subject is required."); return; }
    if (!htmlBody.trim()) { toast.error("Email body is required."); return; }
    if (resolveCount === null) { toast.error("Resolve the audience first."); return; }
    if (resolveCount === 0) { toast.error("Targeting matched zero recipients."); return; }

    // Client-side pre-check — mirrors validate_broadcast_send_request() in
    // core/security_validation.py: subject via validateBroadcastSubject()
    // (CRLF header-injection + XSS), html_body via validateBroadcastHtmlBody()
    // (narrow HTML-tag allowlist for legitimate rich-text bodies), text_body
    // via validateFreeText(). The backend (POST /broadcast/send) remains the
    // authoritative enforcer.
    const subjectCheck = validateBroadcastSubject(subject);
    if (!subjectCheck.isValid) { toast.error(subjectCheck.errors[0]?.message || "Invalid subject"); return; }
    const htmlCheck = validateBroadcastHtmlBody(htmlBody);
    if (!htmlCheck.isValid) { toast.error(htmlCheck.errors[0]?.message || "Invalid email body"); return; }
    if (textBody) {
      const textCheck = validateFreeText(textBody);
      if (!textCheck.isValid) { toast.error(textCheck.errors[0]?.message || "Invalid plain-text body"); return; }
    }

    setConfirmOpen(true);
  }

  async function handleConfirmedSend() {
    setConfirmOpen(false);
    setSending(true);
    try {
      const r = await authFetch(`${API_BASE}/broadcast/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          subject,
          html_body:      htmlBody,
          text_body:      textBody || null,
          enrich_name:    enrichName,
          targeting:      buildTargeting(),
          attachment_ids: attachments.map((a) => a.id),
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        const detail = err.detail;
        const text = typeof detail === "string" ? detail
          : detail?.error === "compliance_blocked"
            ? `Compliance blocked the ${detail.label || "content"} (${(detail.blocked_types || []).join(", ") || "policy"}).`
            : "Broadcast failed.";
        throw new Error(text);
      }
      const d = await r.json();
      toast.success(`Broadcast queued — ${d.total_count} recipients`);
      setDetailId(d.broadcast_id);
      setSubject(""); setTextBody(""); setIntent(""); setHtmlBody(""); setAttachments([]);
      setResolveCount(null); setResolveSample([]);
      loadHistory();
    } catch (e) {
      toast.error(e.message || "Broadcast failed.");
    } finally {
      setSending(false);
    }
  }

  // ── Access guard ─────────────────────────────────────────────────────────
  if (accessAllowed === null) {
    return <div className="p-8 text-center text-gray-400 text-sm">Checking access…</div>;
  }
  if (!accessAllowed) {
    return (
      <div className="p-8 text-center text-gray-400 text-sm">
        Email broadcast access is restricted. Ask an administrator to add your email to{" "}
        <code className="font-mono">BROADCAST_ALLOWED_EMAILS</code>.
      </div>
    );
  }

  if (detailId) {
    return <BroadcastDetail broadcastId={detailId} onBack={() => setDetailId(null)} />;
  }

  // ── Main UI ───────────────────────────────────────────────────────────────
  return (
    <div className="h-full flex flex-col bg-gray-50 overflow-hidden">
      {/* Broadcast confirm modal */}
      {confirmOpen && (
        <BroadcastConfirmModal
          subject={subject}
          resolveCount={resolveCount}
          enrichName={enrichName}
          attachments={attachments.length}
          onConfirm={handleConfirmedSend}
          onCancel={() => setConfirmOpen(false)}
        />
      )}

      {/* Preview modal */}
      {previewOpen && htmlBody && (
        <PreviewModal
          htmlBody={previewHtml || htmlBody}
          previewName={previewName}
          previewEnrich={previewEnrich}
          modelUsed={modelUsed}
          subject={subject}
          onClose={() => setPreviewOpen(false)}
          onNameChange={setPreviewName}
          onEnrichChange={setPreviewEnrich}
        />
      )}

      {/* Page heading — matches Monitoring.jsx style */}
      <div className="bg-white border-b border-gray-200 px-6 py-4 flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-7 h-7 rounded-lg brand-grad-vivid flex items-center justify-center flex-shrink-0 shadow-sm">
            <Mail size={14} className="text-white" />
          </div>
          <div>
            <h1 className="text-sm font-semibold text-indigo-700">Email Broadcast</h1>
            <p className="text-xs text-gray-400">Send rich HTML emails to employees with full audit trail.</p>
          </div>
        </div>
      </div>

      {/* Scrollable content area */}
      <div className="flex-1 overflow-y-auto">
      <div className="p-6 space-y-4">


        {/* ── main form ─────────────────────────────────────────── */}
        <div>

            {/* 1 — Compose */}
            <section className="bg-white border border-gray-200 rounded-xl shadow-md overflow-hidden mb-4">
              <div className="flex items-center gap-2 px-5 py-4 border-b border-gray-100">
                <div className="w-5 h-5 rounded-md brand-grad-vivid flex items-center justify-center flex-shrink-0 shadow-sm">
                  <Sparkles size={11} className="text-white" />
                </div>
                <span className="text-sm font-semibold text-gray-800">Compose</span>
              </div>
              <div className="p-5">
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">
                    What should this email say?
                  </label>
                  <textarea
                    rows={3}
                    value={intent}
                    onChange={(e) => { setIntent(e.target.value); if (intentError) setIntentError(""); }}
                    placeholder="e.g. Announce the Friday 4pm AI town-hall in the auditorium with snacks and a Q&A session."
                    className={`w-full border rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${intentError ? "border-red-400 bg-red-50/30" : "border-gray-300"}`}
                  />
                  {intentError && <p className="mt-1 text-xs text-red-600">{intentError}</p>}
                </div>

                {/* Tone + Actions toolbar */}
                <div className="flex items-center gap-2 flex-wrap">

                  {/* Tone selector */}
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <span className="text-xs font-medium text-gray-400 whitespace-nowrap">Tone</span>
                    <div className="relative">
                      <select
                        value={tone}
                        onChange={(e) => setTone(e.target.value)}
                        className="appearance-none pl-3 pr-7 py-1.5 text-xs font-medium text-gray-700 bg-white border border-gray-200 rounded-lg focus:outline-none focus:border-indigo-300 cursor-pointer hover:border-gray-300 transition-colors shadow-sm"
                      >
                        <option value="professional">Professional</option>
                        <option value="friendly">Friendly</option>
                        <option value="formal">Formal</option>
                        <option value="urgent">Urgent</option>
                        <option value="celebratory">Celebratory</option>
                      </select>
                      <ChevronDown size={11} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
                    </div>
                  </div>

                  {/* Generate / Regenerate */}
                  <button
                    onClick={handleGenerate}
                    disabled={generating}
                    className="inline-flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold text-white rounded-lg brand-grad hover:opacity-85 disabled:opacity-50 disabled:cursor-not-allowed transition-all shadow-sm cursor-pointer flex-shrink-0"
                  >
                    {generating ? <RefreshCw size={12} className="animate-spin" /> : <Sparkles size={12} />}
                    {htmlBody ? "Regenerate" : "Generate template"}
                  </button>

                  {/* Preview — only when htmlBody exists */}
                  {htmlBody && (
                    <>
                      <button
                        onClick={() => setPreviewOpen(true)}
                        className="inline-flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold text-indigo-600 bg-white border border-indigo-200 rounded-lg hover:bg-indigo-50 hover:border-indigo-300 transition-all shadow-sm cursor-pointer flex-shrink-0"
                      >
                        <Eye size={12} className="flex-shrink-0" />
                        Preview
                      </button>
                    </>
                  )}

                  {/* Spacer + model badge */}
                  {modelUsed && (
                    <span className="text-xs text-gray-400 font-mono">{modelUsed}</span>
                  )}
                </div>

                {htmlBody && (
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">
                      HTML (editable)
                    </label>
                    <textarea
                      rows={8}
                      value={htmlBody}
                      onChange={(e) => setHtmlBody(e.target.value)}
                      className={`${INPUT_CLS} font-mono text-xs`}
                    />
                    <p className="text-xs text-gray-400 mt-1">
                      Use <code className="font-mono bg-gray-100 px-1 rounded">{"{{name}}"}</code> as a placeholder for the recipient's first name.
                    </p>
                  </div>
                )}
              </div>
            </div>
            </section>

            {/* 2 & 3 — Targeting + Options + Send — flush with Compose */}
            <div className="mb-4">

              {/* Top row: Targeting | Options — gap matches mb-4 rhythm */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">

                {/* ── 2 Targeting ─────────────────────────────────────────── */}
                <section className="flex flex-col bg-white rounded-xl border border-gray-200 shadow-md overflow-hidden">
                  {/* Card header */}
                  <div className="flex items-center gap-2 px-5 py-4 border-b border-gray-100">
                    <div className="w-5 h-5 rounded-md brand-grad-vivid flex items-center justify-center flex-shrink-0 shadow-sm">
                      <Users size={11} className="text-white" />
                    </div>
                    <span className="text-sm font-semibold text-gray-800">Targeting</span>
                  </div>

                  {/* Card body — scrollable area capped so resolve table doesn't blow height */}
                  <div className="flex-1 overflow-y-auto p-5 space-y-4" style={{ maxHeight: 480 }}>
                    <label className="inline-flex items-center gap-2 text-sm text-gray-700 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={targetAll}
                        onChange={(e) => setTargetAll(e.target.checked)}
                        className="rounded border-gray-300 accent-indigo-600 focus:ring-indigo-500"
                      />
                      All active employees
                    </label>

                    {/* Departments */}
                    <div>
                      <label className="block text-xs font-medium text-gray-600 mb-1">Departments</label>
                      {!departmentsLoaded ? (
                        <div className="text-xs text-gray-400">Loading departments…</div>
                      ) : departments.length === 0 ? (
                        <div className="text-xs text-gray-400">No departments found.</div>
                      ) : (
                        <div className="relative" ref={deptDropdownRef}>
                          <button
                            type="button"
                            onClick={() => setDeptDropdownOpen((v) => !v)}
                            className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm bg-white text-left flex items-center justify-between gap-2 cursor-pointer hover:bg-gray-50 focus:outline-none focus:border-indigo-300"
                          >
                            <span className="truncate text-gray-700">
                              {selectedDepts.length === 0 ? "Select departments…" : `${selectedDepts.length} selected`}
                            </span>
                            <ChevronDown size={15} className={"text-gray-400 transition-transform " + (deptDropdownOpen ? "rotate-180" : "")} />
                          </button>

                          {selectedDepts.length > 0 && (
                            <div className="flex flex-wrap gap-2 mt-2.5">
                              {selectedDepts.map((d) => (
                                <span key={d} className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium brand-grad text-white shadow-sm">
                                  {d}
                                  <button type="button" onClick={() => toggleDept(d)} className="text-white/90 hover:text-white transition-colors cursor-pointer" aria-label={`Remove ${d}`}>
                                    <X size={11} />
                                  </button>
                                </span>
                              ))}
                            </div>
                          )}

                          {deptDropdownOpen && (
                            <div className="absolute z-20 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden">
                              <div className="p-2 border-b border-gray-100 relative">
                                <Search size={13} className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
                                <input
                                  type="text"
                                  autoFocus
                                  value={deptSearch}
                                  onChange={(e) => setDeptSearch(e.target.value)}
                                  placeholder="Search departments…"
                                  className="w-full pl-7 pr-2 py-1.5 border border-gray-300 rounded-md text-sm text-gray-900 focus:outline-none focus:border-indigo-300"
                                />
                              </div>
                              <div className="max-h-48 overflow-y-auto">
                                {(() => {
                                  const q = deptSearch.trim().toLowerCase();
                                  const filtered = q ? departments.filter((d) => d.toLowerCase().includes(q)) : departments;
                                  if (filtered.length === 0)
                                    return <div className="px-3 py-3 text-xs text-gray-400 text-center">No matching departments.</div>;
                                  return filtered.map((d) => {
                                    const active = selectedDepts.includes(d);
                                    return (
                                      <button
                                        key={d}
                                        type="button"
                                        onClick={() => toggleDept(d)}
                                        className={"w-full text-left px-3 py-2 text-sm flex items-center gap-2 cursor-pointer text-gray-700 hover:bg-gray-50"}
                                      >
                                        <span className={"w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 " + (active ? "brand-grad border-transparent" : "bg-white border-gray-300")}>
                                          {active && <Check size={10} className="text-white" />}
                                        </span>
                                        <span className="truncate">{d}</span>
                                      </button>
                                    );
                                  });
                                })()}
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                      {(selectedDepts.length > 0 || maxAdLevel !== "") && (
                        <p className="text-xs text-gray-400 mt-2">
                          {selectedDepts.length > 0 && maxAdLevel !== ""
                            ? `Filtering ${selectedDepts.length} dept${selectedDepts.length === 1 ? "" : "s"} with AD level ≤ ${maxAdLevel}.`
                            : selectedDepts.length > 0
                              ? `Filtering ${selectedDepts.length} dept${selectedDepts.length === 1 ? "" : "s"}.`
                              : `Filtering employees with AD level ≤ ${maxAdLevel}.`}
                        </p>
                      )}
                    </div>

                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className="block text-xs font-medium text-gray-600 mb-1">Include up to AD level</label>
                        <input
                          type="number"
                          min={0} max={6}
                          value={maxAdLevel}
                          onChange={(e) => setMaxAdLevel(e.target.value)}
                          placeholder="e.g. 3"
                          className={INPUT_CLS}
                        />
                        <p className="text-xs text-gray-400 mt-1">0 = exec · 6 = junior</p>
                      </div>
                      <div ref={userSearchRef}>
                        <label className="block text-xs font-medium text-gray-600 mb-1">Add individuals</label>
                        <div className="relative">
                          <input
                            type="text"
                            value={userSearch}
                            onChange={(e) => setUserSearch(e.target.value)}
                            placeholder="Search by name or email…"
                            className={INPUT_CLS}
                          />
                          {userResults.length > 0 && (
                            <div className="absolute z-20 top-full left-0 right-0 mt-1 border border-gray-200 rounded-lg max-h-48 overflow-y-auto bg-white shadow-lg">
                              {userResults.map((u) => (
                                <button
                                  key={u.id}
                                  type="button"
                                  onClick={() => addIndividual(u)}
                                  className="w-full text-left px-3 py-2 hover:bg-indigo-50 text-sm border-b last:border-b-0 border-gray-100 cursor-pointer"
                                >
                                  <div className="font-medium text-gray-900 text-xs">{u.name}</div>
                                  <div className="text-xs text-gray-400">{u.email}{u.department ? ` · ${u.department}` : ""}</div>
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                        {userSearchLoading && <div className="text-xs text-gray-400 mt-1">Searching…</div>}
                      </div>
                    </div>

                    {individuals.length > 0 && (
                      <div>
                        <label className="block text-xs font-medium text-gray-600 mb-1">Selected individuals</label>
                        <div className="flex flex-wrap gap-1.5 max-h-20 overflow-y-auto">
                          {individuals.map((u) => (
                            <span key={u.email} className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs bg-indigo-50 text-indigo-700 border border-indigo-100">
                              {u.name || u.email}
                              <button type="button" onClick={() => removeIndividual(u.email)} className="hover:text-red-500 cursor-pointer" aria-label={`Remove ${u.email}`}>
                                <X size={11} />
                              </button>
                            </span>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Resolve row */}
                    <div className="flex items-center gap-2 pt-1">
                      <button onClick={handleResolve} disabled={resolving} className={GRADIENT_BTN}>
                        {resolving ? <RefreshCw size={14} className="animate-spin" /> : <Users size={14} />}
                        {resolving ? "Resolving…" : "Resolve audience"}
                      </button>
                      {resolveCount > 0 && (
                        <button
                          type="button"
                          onClick={() => { setResolveCount(null); setResolveSample([]); setTargetAll(false); setSelectedDepts([]); setIndividuals([]); setMaxAdLevel(""); }}
                          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-white border border-gray-300 text-xs font-medium text-gray-600 hover:bg-gray-50 hover:border-gray-300 hover:text-gray-600 transition-colors cursor-pointer disabled:opacity-50"
                        >
                           Clear
                        </button>
                      )}
                    </div>

                    {/* Sample table — fixed height, never grows the column */}
                    {resolveSample.length > 0 && (
                      <div className="border border-gray-200 rounded-lg overflow-hidden">
                        <div className="bg-gray-50 px-3 py-2 border-b border-gray-200 flex items-center justify-between">
                          <span className="text-xs font-semibold text-gray-500">Recipients List</span>
                          <span className="text-xs text-gray-400">
                            {resolveSample.length} of {resolveCount}{resolveCount > resolveSample.length ? " shown" : ""}
                          </span>
                        </div>
                        <div className="overflow-y-auto" style={{ maxHeight: 180 }}>
                          <table className="w-full text-xs">
                            <thead className="bg-gray-50 text-gray-400 uppercase tracking-wide sticky top-0 z-10">
                              <tr>
                                <th className="px-3 py-2 text-left font-medium">Name</th>
                                <th className="px-3 py-2 text-left font-medium">Email</th>
                                <th className="px-3 py-2 text-left font-medium">Dept</th>
                                <th className="px-3 py-2 text-center font-medium">AD</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-gray-100 bg-white">
                              {resolveSample.map((r) => (
                                <tr key={r.email} className="hover:bg-gray-50">
                                  <td className="px-3 py-1.5 text-gray-800 font-medium truncate max-w-[90px]">{r.name || "—"}</td>
                                  <td className="px-3 py-1.5 text-gray-500 truncate max-w-[110px]">{r.email}</td>
                                  <td className="px-3 py-1.5 text-gray-500 truncate max-w-[80px]">{r.department || "—"}</td>
                                  <td className="px-3 py-1.5 text-gray-500 text-center">{r.ad_level != null ? r.ad_level : "—"}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    )}
                  </div>
                </section>

                {/* ── 3 Options ───────────────────────────────────────────── */}
                <section className="flex flex-col bg-white rounded-xl border border-gray-200 shadow-md overflow-hidden">
                  {/* Card header */}
                  <div className="flex items-center gap-2 px-5 py-4 border-b border-gray-100">
                    <div className="w-5 h-5 rounded-md brand-grad-vivid flex items-center justify-center flex-shrink-0 shadow-sm">
                      <Paperclip size={11} className="text-white" />
                    </div>
                    <span className="text-sm font-semibold text-gray-800">Options</span>
                  </div>

                  {/* Card body */}
                  <div className="flex-1 overflow-y-auto p-5 space-y-4" style={{ maxHeight: 480 }}>
                    <div>
                      <label className="block text-xs font-medium text-gray-600 mb-1">Subject <span className="text-red-400">*</span></label>
                      <input
                        type="text"
                        value={subject}
                        onChange={(e) => setSubject(e.target.value)}
                        placeholder="e.g. AI Town-Hall — Friday 4 PM (Auditorium)"
                        className={INPUT_CLS}
                      />
                    </div>

                    <div>
                      <label className="block text-xs font-medium text-gray-600 mb-1">Plain-text fallback <span className="text-gray-400 font-normal">(optional)</span></label>
                      <textarea
                        rows={3}
                        value={textBody}
                        onChange={(e) => setTextBody(e.target.value)}
                        placeholder="Shown to mail clients that don't render HTML."
                        className={`${INPUT_CLS} resize-none`}
                      />
                    </div>

                    <label className="inline-flex items-start gap-2 text-sm text-gray-700 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={enrichName}
                        onChange={(e) => setEnrichName(e.target.checked)}
                        className="rounded border-gray-300 accent-indigo-600 focus:ring-indigo-500 mt-0.5 flex-shrink-0"
                      />
                      <span>Enrich with user name <span className="text-gray-400 text-xs">(replaces <code className="font-mono bg-gray-100 px-1 rounded">{"{{name}}"}</code> per recipient)</span></span>
                    </label>

                    {/* Attachments */}
                    <div>
                      <label className="block text-xs font-medium text-gray-600 mb-2">Attachments</label>
                      {uploadError && (
                        <div className="flex items-start gap-2 px-3 py-2 mb-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-600">
                          <XCircle size={13} className="flex-shrink-0 mt-0.5" />
                          <span className="flex-1">{uploadError}</span>
                          <button type="button" onClick={() => setUploadError(null)} className="flex-shrink-0 text-red-400 hover:text-red-600 transition-colors cursor-pointer">
                            <X size={12} />
                          </button>
                        </div>
                      )}
                      {/* Drop zone */}
                      <div
                        onDragOver={(e) => e.preventDefault()}
                        onDrop={(e) => { e.preventDefault(); handleUpload(Array.from(e.dataTransfer.files || [])); }}
                        onClick={() => fileInputRef.current?.click()}
                        className="border border-dashed border-gray-300 rounded-lg px-4 py-3 text-center text-sm text-gray-500 bg-gray-50 hover:bg-indigo-50/40 hover:border-indigo-300 transition-colors cursor-pointer"
                      >
                        <Paperclip size={15} className="mx-auto mb-1 text-gray-400" />
                        <span>Drag &amp; drop or <span className="text-indigo-600 font-medium underline">browse</span></span>
                        <div className="text-xs text-gray-400 mt-0.5">pdf · docx · png · jpg · csv · xlsx · max 25 MB</div>
                        <input ref={fileInputRef} type="file" multiple className="hidden" onChange={(e) => handleUpload(Array.from(e.target.files || []))} />
                        {uploading && <div className="text-xs text-indigo-500 mt-1 font-medium">Uploading…</div>}
                      </div>

                      {/* Attached files — fixed height list, scrollable */}
                      {attachments.length > 0 && (
                        <div className="mt-2 border border-gray-200 rounded-lg overflow-hidden">
                          <div className="bg-gray-50 px-3 py-1.5 border-b border-gray-100 flex items-center justify-between">
                            <span className="text-xs font-medium text-gray-500">{attachments.length} file{attachments.length === 1 ? "" : "s"} attached</span>
                          </div>
                          <div className="overflow-y-auto divide-y divide-gray-100" style={{ maxHeight: 140 }}>
                            {attachments.map((a) => (
                              <div key={a.id} className="flex items-center gap-2 px-3 py-2 hover:bg-gray-50">
                                <Paperclip size={12} className="text-gray-400 flex-shrink-0" />
                                <span className="text-xs text-gray-700 truncate flex-1">{a.filename}</span>
                                <span className="text-xs text-gray-400 flex-shrink-0">{fmtBytes(a.size_bytes)}</span>
                                <button
                                  type="button"
                                  onClick={() => handleRemoveAttachment(a.id)}
                                  className="text-gray-400 hover:text-red-500 flex-shrink-0 transition-colors cursor-pointer"
                                  aria-label={`Remove ${a.filename}`}
                                >
                                  <Trash2 size={12} />
                                </button>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                </section>

              </div>{/* end top grid */}

              {/* ── 4 Send Broadcast ─────────────────────────────────── */}
              {(() => {
                const allReady = !!subject.trim() && !!htmlBody.trim() && resolveCount > 0;
                return (
                  <section className="bg-white rounded-xl overflow-hidden border border-gray-200 shadow-md">

                    {/* Header — matches other section headers exactly */}
                    <div className="flex items-center gap-2 px-5 py-4 border-b border-gray-100">
                      <div className="w-5 h-5 rounded-md brand-grad-vivid flex items-center justify-center flex-shrink-0 shadow-sm">
                        <Send size={11} className="text-white" />
                      </div>
                      <span className="text-sm font-semibold text-gray-800">Send Broadcast</span>
                    </div>

                    {/* Body */}
                    <div className="px-5 py-4 space-y-4">

                      {/* Pre-flight steps — horizontal pill row */}
                      <div className="flex items-center gap-2 flex-wrap">
                        {[
                          {
                            label: "Email body",
                            done: !!htmlBody.trim(),
                            hint: "Generate or write in Compose",
                          },
                          {
                            label: subject.trim() ? `"${subject.length > 28 ? subject.slice(0, 28) + "…" : subject}"` : "Subject",
                            done: !!subject.trim(),
                            hint: "Enter subject in Options",
                          },
                          {
                            label: resolveCount > 0
                              ? `${resolveCount} recipient${resolveCount === 1 ? "" : "s"}`
                              : "Audience",
                            done: resolveCount > 0,
                            hint: resolveCount === null ? "Resolve in Targeting" : "No recipients matched",
                          },
                        ].map(({ label, done, hint }) => (
                          <div
                            key={label}
                            title={!done ? hint : undefined}
                            className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border transition-all
                              ${done
                                ? "bg-green-50 border-green-200 text-green-700"
                                : "bg-gray-50 border-gray-200 text-gray-400"}`}
                          >
                            {done
                              ? <CheckCircle2 size={11} className="text-green-500 flex-shrink-0" />
                              : <div className="w-2.5 h-2.5 rounded-full border-2 border-gray-300 flex-shrink-0" />}
                            {label}
                          </div>
                        ))}

                        {/* Optional metadata when all ready */}
                        {allReady && attachments.length > 0 && (
                          <div className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border bg-indigo-50 border-indigo-100 text-indigo-600">
                            <Paperclip size={10} className="flex-shrink-0" />
                            {attachments.length} attachment{attachments.length === 1 ? "" : "s"}
                          </div>
                        )}
                        {allReady && enrichName && (
                          <div className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border bg-indigo-50 border-indigo-100 text-indigo-600">
                            <Sparkles size={10} className="flex-shrink-0" />
                            Personalised
                          </div>
                        )}
                      </div>

                      {/* CTA row */}
                      <div className="flex items-center justify-between gap-4">
                        <p className="text-xs text-gray-400">
                          {allReady
                            ? "All checks passed — one email will be queued per recipient."
                            : "Complete the steps above to unlock sending."}
                        </p>
                        <button
                          onClick={handleSend}
                          disabled={sending || !allReady}
                          className={`flex-shrink-0 inline-flex items-center gap-2 px-6 py-2.5 rounded-xl text-sm font-semibold transition-all duration-200
                            ${allReady
                              ? "brand-grad text-white shadow-md hover:shadow-lg hover:opacity-90 cursor-pointer"
                              : "bg-gray-100 text-gray-400 cursor-not-allowed"}`}
                        >
                          {sending ? <RefreshCw size={14} className="animate-spin" /> : <Send size={14} />}
                          {sending ? "Sending…" : "Send broadcast"}
                        </button>
                      </div>
                    </div>
                  </section>
                );
              })()}

            </div>{/* end section group */}

        </div>{/* end main form */}

        {/* ── History ──────────────────────────────────────────────────── */}
        {(() => {
          const filtered = history.filter((b) =>
            !historySearch.trim() || b.subject.toLowerCase().includes(historySearch.trim().toLowerCase())
          );
          const totalPages = Math.ceil(filtered.length / HISTORY_PAGE_SIZE);
          const paginated  = filtered.slice(historyPage * HISTORY_PAGE_SIZE, (historyPage + 1) * HISTORY_PAGE_SIZE);

          return (
          <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-md">

            {/* Header */}
            <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between gap-3 flex-wrap">
              <div className="flex items-center gap-2">
                <div className="w-5 h-5 rounded-md brand-grad-vivid flex items-center justify-center flex-shrink-0 shadow-sm">
                  <Clock size={11} className="text-white" />
                </div>
                <div>
                  <h3 className="text-sm font-semibold text-gray-800 leading-none">Broadcast History</h3>
                  <p className="text-xs text-gray-400 mt-0.5">{filtered.length} broadcast{filtered.length === 1 ? "" : "s"}</p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {/* Search */}
                <div className="relative">
                  <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
                  <input
                    type="text"
                    value={historySearch}
                    onChange={(e) => { setHistorySearch(e.target.value); setHistoryPage(0); }}
                    placeholder="Search broadcasts…"
                    className="pl-7 pr-7 py-1.5 text-xs border border-gray-200 rounded-lg focus:outline-none focus:border-indigo-300 text-gray-700 placeholder-gray-400 w-48"
                  />
                  {historySearch && (
                    <button
                      type="button"
                      onClick={() => { setHistorySearch(""); setHistoryPage(0); }}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 transition-colors cursor-pointer"
                    >
                      <X size={12} />
                    </button>
                  )}
                </div>
                <button
                  onClick={() => { setHistorySearch(""); setHistoryPage(0); loadHistory(); }}
                  disabled={historyLoading}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-gray-200 text-xs text-gray-500 hover:bg-gray-50 hover:text-gray-700 transition-colors disabled:opacity-50 cursor-pointer disabled:cursor-not-allowed"
                >
                  <RefreshCw size={12} className={historyLoading ? "animate-spin" : ""} />
                  Refresh
                </button>
              </div>
            </div>

            {/* Table */}
            <div style={{ minHeight: 260 }}>
            <table className="w-full text-sm">
              <thead className="bg-white text-gray-400 text-xs uppercase tracking-wide border-b border-gray-100">
                <tr>
                  <th className="px-5 py-3 text-left font-semibold">Subject</th>
                  <th className="px-4 py-3 text-left font-semibold">Status</th>
                  <th className="px-4 py-3 text-center font-semibold">Total</th>
                  <th className="px-4 py-3 text-center font-semibold">Sent</th>
                  <th className="px-4 py-3 text-center font-semibold">Failed</th>
                  <th className="px-4 py-3 text-left font-semibold">Created</th>
                  <th className="px-5 py-3 text-right font-semibold">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 bg-white">
                {historyLoading && history.length === 0 ? (
                  <tr><td colSpan={7} className="px-5 py-10 text-center text-gray-400 text-sm">Loading…</td></tr>
                ) : paginated.length === 0 ? (
                  <tr><td colSpan={7} className="px-5 py-10 text-center">
                    <div className="flex flex-col items-center gap-2">
                      <Search size={22} className="text-gray-300" />
                      <p className="text-sm font-medium text-gray-400">
                        {historySearch ? `No results for "${historySearch}"` : "No broadcasts yet."}
                      </p>
                      {historySearch && (
                        <button
                          type="button"
                          onClick={() => { setHistorySearch(""); setHistoryPage(0); }}
                          className="text-xs text-indigo-500 hover:text-indigo-700 underline cursor-pointer"
                        >
                          Clear search
                        </button>
                      )}
                    </div>
                  </td></tr>
                ) : (
                  paginated.map((b) => (
                    <tr key={b.id} className="hover:bg-indigo-50/30 transition-colors group">
                      <td className="px-5 py-3 max-w-xs">
                        <span className="block truncate text-gray-800 text-sm font-medium group-hover:text-indigo-700 transition-colors" title={b.subject}>{b.subject}</span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`${BADGE_BASE} ${STATUS_BADGE[b.status] || STATUS_BADGE.draft}`}>
                          {STATUS_ICON[b.status]}{b.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-center text-gray-600 text-sm font-medium">{b.total_count}</td>
                      <td className="px-4 py-3 text-center text-green-600 text-sm font-semibold">{b.success_count}</td>
                      <td className="px-4 py-3 text-center text-red-500 text-sm font-semibold">{b.failure_count}</td>
                      <td className="px-4 py-3 text-gray-400 text-xs whitespace-nowrap">{fmtDate(b.created_at)}</td>
                      <td className="px-5 py-3 text-right">
                        <button
                          onClick={() => setDetailId(b.id)}
                          className="cursor-pointer p-1.5 rounded-lg text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 transition-all duration-200"
                          title="View details"
                        >
                          <Eye size={15} />
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
            </div>{/* end minHeight wrapper */}

            {/* Pagination */}
            {filtered.length > HISTORY_PAGE_SIZE && (
              <div className="px-5 py-3 border-t border-gray-100 flex items-center justify-between gap-3">
                <span className="text-xs text-gray-400">
                  Showing <span className="font-semibold text-gray-600">{historyPage * HISTORY_PAGE_SIZE + 1}–{Math.min((historyPage + 1) * HISTORY_PAGE_SIZE, filtered.length)}</span> of <span className="font-semibold text-gray-600">{filtered.length}</span>
                </span>
                <div className="flex items-center gap-1.5">
                  <button
                    onClick={() => setHistoryPage((p) => Math.max(0, p - 1))}
                    disabled={historyPage === 0}
                    className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-semibold border border-gray-200 bg-white text-gray-600 hover:bg-indigo-50 hover:border-indigo-200 hover:text-indigo-600 disabled:opacity-40 disabled:cursor-not-allowed transition-all cursor-pointer"
                  >
                    <ChevronDown size={12} className="rotate-90" /> Previous
                  </button>
                  {/* Page dots */}
                  <div className="flex items-center gap-1">
                    {Array.from({ length: totalPages }).map((_, i) => (
                      <button
                        key={i}
                        onClick={() => setHistoryPage(i)}
                        className={`transition-all duration-150 rounded-full cursor-pointer ${
                          i === historyPage
                            ? "w-5 h-2 brand-grad-r"
                            : "w-2 h-2 bg-gray-200 hover:bg-indigo-300"
                        }`}
                      />
                    ))}
                  </div>
                  <button
                    onClick={() => setHistoryPage((p) => Math.min(totalPages - 1, p + 1))}
                    disabled={historyPage >= totalPages - 1}
                    className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-semibold border border-transparent brand-grad text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed transition-all cursor-pointer shadow-sm"
                  >
                    Next <ChevronDown size={12} className="-rotate-90" />
                  </button>
                </div>
              </div>
            )}

          </div>
          );
        })()}
        {/* end history card */}

      </div>{/* end p-6 space-y-4 */}
      </div>{/* end scrollable area */}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────────────
// Broadcast detail view
// ──────────────────────────────────────────────────────────────────────────────
function BroadcastDetail({ broadcastId, onBack }) {
  const { toast } = useToast();
  const [data,         setData]         = useState(null);
  const [recipients,   setRecipients]   = useState({ total: 0, items: [] });
  const [statusFilter, setStatusFilter] = useState("");
  const [loading,      setLoading]      = useState(true);
  const [cancelling,   setCancelling]   = useState(false);
  const [page,         setPage]         = useState(0);
  const PAGE_SIZE = 100;

  // PII payload encryption flag — see the top-level EmailBroadcast component
  // for the full explanation. Fetched once per BroadcastDetail mount.
  const piiEnabledPromise = useRef(
    apiFetch(`${API_BASE}/auth/ui-config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => !!d?.pii_payload_encryption_enabled)
      .catch(() => false)
  );

  const load = () => {
    setLoading(true);
    Promise.all([
      authFetch(`${API_BASE}/broadcast/${broadcastId}`).then((r) => (r.ok ? r.json() : null)),
      authFetch(
        `${API_BASE}/broadcast/${broadcastId}/recipients?` +
        `limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}` +
        (statusFilter ? `&status=${statusFilter}` : ""),
      ).then((r) => (r.ok ? r.json() : { total: 0, items: [] })),
    ])
      .then(async ([d, rcpt]) => {
        const piiOn = await piiEnabledPromise.current;
        if (d) {
          d.created_by_email = await decryptPii(d.created_by_email, piiOn);
          d.failed_sample = await Promise.all((d.failed_sample || []).map(async (f) => ({
            ...f,
            email: await decryptPii(f.email, piiOn),
            name:  await decryptPii(f.name,  piiOn),
          })));
          setData(d);
        }
        const items = await Promise.all((rcpt?.items || []).map(async (r) => ({
          ...r,
          email: await decryptPii(r.email, piiOn),
          name:  await decryptPii(r.name,  piiOn),
        })));
        setRecipients({ total: rcpt?.total || 0, items });
      })
      .finally(() => setLoading(false));
  };

  useEffect(load, [broadcastId, page, statusFilter]);

  useEffect(() => {
    if (!data || data.status !== "sending") return;
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.status, broadcastId, page, statusFilter]);

  async function handleCancel() {
    setCancelling(true);
    try {
      const r = await authFetch(`${API_BASE}/broadcast/${broadcastId}/cancel`, { method: "POST" });
      if (r.ok) {
        load();
      } else {
        const err = await r.json().catch(() => ({}));
        toast.error(err.detail || "Could not cancel broadcast.");
      }
    } catch {
      toast.error("Could not cancel broadcast.");
    } finally {
      setCancelling(false);
    }
  }

  if (loading && !data) return (
    <div className="h-full flex items-center justify-center bg-gray-50">
      <div className="flex items-center gap-2 text-gray-400 text-sm">
        <RefreshCw size={14} className="animate-spin" /> Loading broadcast…
      </div>
    </div>
  );
  if (!data) return (
    <div className="h-full flex flex-col items-center justify-center bg-gray-50 gap-3">
      <XCircle size={32} className="text-red-300" />
      <p className="text-sm text-gray-500">Could not load broadcast.</p>
      <button onClick={onBack} className="text-xs text-indigo-600 hover:text-indigo-800 font-medium underline cursor-pointer">← Back</button>
    </div>
  );

  return (
    <div className="h-full flex flex-col bg-gray-50 overflow-hidden">
      <style>{`
        @keyframes dotPulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50%       { opacity: 0.4; transform: scale(0.75); }
        }
        @keyframes ping {
          0%        { transform: scale(1); opacity: 0.6; }
          80%, 100% { transform: scale(2); opacity: 0; }
        }
        @keyframes badgePulse {
          0%, 100% { box-shadow: 0 0 0 0 rgba(239,68,68,0); }
          50%       { box-shadow: 0 0 0 5px rgba(239,68,68,0.12); }
        }
      `}</style>

      {/* ── Header — sleek, no breadcrumb ── */}
      <div className="bg-white border-b border-gray-200 px-6 py-4 flex items-center justify-between gap-4 flex-shrink-0">
        <div className="flex items-center gap-3 min-w-0">
          {/* Back button */}
          <button
            onClick={onBack}
            className="w-8 h-8 flex items-center justify-center rounded-lg border border-gray-200 text-gray-400 hover:text-indigo-600 hover:border-indigo-200 hover:bg-indigo-50 transition-all flex-shrink-0 cursor-pointer"
          >
            <ChevronDown size={14} className="rotate-90" />
          </button>
          {/* Icon */}
          <div className="w-8 h-8 rounded-lg brand-grad-vivid flex items-center justify-center flex-shrink-0 shadow-sm">
            <Mail size={14} className="text-white" />
          </div>
          {/* Title + meta */}
          <div className="min-w-0">
            <h1 className="text-sm font-semibold text-indigo-700 truncate leading-none">{data.subject}</h1>
            <div className="flex items-center gap-2 mt-1 flex-wrap">
              <span className={`${BADGE_BASE} ${STATUS_BADGE[data.status] || STATUS_BADGE.draft}`}>
                {STATUS_ICON[data.status]}{data.status}
              </span>
              <span className="text-xs text-gray-400">{fmtDate(data.created_at)}</span>
              {data.created_by_email && <span className="text-xs text-gray-400">· {data.created_by_email}</span>}
              {data.model_used && <span className="text-xs font-mono text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">{data.model_used}</span>}
            </div>
          </div>
        </div>
        {(data.status === "queued" || data.status === "sending") && (
          <button
            onClick={handleCancel}
            disabled={cancelling}
            className="flex-shrink-0 inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold cursor-pointer disabled:cursor-not-allowed
               border border-red-100 text-red-500 disabled:opacity-50"
            style={{ animation: cancelling ? "none" : "badgePulse 1.5s ease-in-out infinite" }}
          >
            {cancelling ? (
              <>
                <RefreshCw size={11} className="animate-spin" />
                <span>Cancelling…</span>
              </>
            ) : (
              <>
                {/* Live pulsing dot */}
                <span className="relative flex h-2 w-2 flex-shrink-0">
                  <span className="absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-60" style={{ animation: "ping 1.5s ease-in-out infinite" }} />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-red-500" style={{ animation: "dotPulse 1.5s ease-in-out infinite" }} />
                </span>
                <span>Sending</span>
                <span className="w-px h-3 bg-red-200 flex-shrink-0" />
                <span className="font-medium">Cancel</span>
              </>
            )}
          </button>
        )}
      </div>

      {/* ── Fixed content — no page scroll ── */}
      <div className="flex-1 flex flex-col min-h-0 p-6">

        {/* Single white container wrapping stats + table */}
        <div className="flex-1 min-h-0 flex flex-col bg-white border border-gray-200 rounded-xl shadow-md overflow-hidden">

        {/* Stats row */}
        <div className="grid grid-cols-3 gap-4 p-5 border-b border-gray-100 flex-shrink-0">
          {[
            { label: "Total Recipients", value: data.total_count,   icon: Users,        color: "indigo" },
            { label: "Delivered",        value: data.success_count, icon: CheckCircle2, color: "green"  },
            { label: "Failed",           value: data.failure_count, icon: XCircle,      color: "red"    },
          ].map(({ label, value, icon: Icon, color }) => {
            const styles = {
              indigo: { card: "bg-indigo-50 border-indigo-100", icon: "from-indigo-600 to-violet-600", val: "text-indigo-700", lbl: "text-indigo-400" },
              green:  { card: "bg-green-50  border-green-100",  icon: "from-green-500  to-emerald-500", val: "text-green-700",  lbl: "text-green-400"  },
              red:    { card: "bg-red-50    border-red-100",    icon: "from-red-500    to-rose-500",    val: "text-red-600",    lbl: "text-red-400"    },
            }[color];
            return (
              <div key={label} className={`rounded-xl border ${styles.card} p-4 flex-shrink-0`}>
                <div className="flex items-center justify-between mb-3">
                  <span className={`text-xs font-medium ${styles.lbl}`}>{label}</span>
                  <div className={`w-7 h-7 rounded-lg bg-gradient-to-br ${styles.icon} flex items-center justify-center shadow-sm`}>
                    <Icon size={13} className="text-white" />
                  </div>
                </div>
                <p className={`text-3xl font-bold ${styles.val}`}>{value ?? "—"}</p>
              </div>
            );
          })}
        </div>

        {/* Recipients section — fills remaining height */}
        <div className="flex-1 min-h-0 flex flex-col overflow-hidden">

          {/* Card header */}
          <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between flex-wrap gap-3 flex-shrink-0">
            <div className="flex items-center gap-2">
              <div className="w-6 h-6 rounded-lg brand-grad-vivid flex items-center justify-center shadow-sm">
                <Users size={12} className="text-white" />
              </div>
              <div>
                <p className="text-sm font-semibold text-gray-800 leading-none">Recipients</p>
                <p className="text-xs text-gray-400 mt-0.5">{recipients.total} total</p>
              </div>
            </div>
            <div className="flex items-center gap-1.5 flex-wrap">
              {["", "pending", "sent", "failed", "skipped"].map((s) => (
                <button
                  key={s || "all"}
                  onClick={() => { setStatusFilter(s); setPage(0); }}
                  className={`px-2.5 py-1 rounded-full text-xs font-medium border cursor-pointer transition-all ${
                    statusFilter === s
                      ? "brand-grad text-white border-transparent shadow-sm"
                      : "bg-white text-gray-500 border-gray-200 hover:border-indigo-200 hover:text-indigo-600"
                  }`}
                >
                  {s || "All"}
                </button>
              ))}
            </div>
          </div>

          {/* Table — fills card, scrolls internally */}
          <div className="flex-1 overflow-y-auto min-h-0">
            <table className="w-full text-sm">
              <thead className="bg-white text-gray-400 text-xs uppercase tracking-wide border-b border-gray-100 sticky top-0 z-10">
                <tr>
                  <th className="px-5 py-3 text-left font-semibold">Name</th>
                  <th className="px-4 py-3 text-left font-semibold">Email</th>
                  <th className="px-4 py-3 text-left font-semibold">Status</th>
                  <th className="px-4 py-3 text-left font-semibold">Sent at</th>
                  <th className="px-5 py-3 text-left font-semibold">Error</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 bg-white">
                {recipients.items.length === 0 ? (
                  <tr><td colSpan={5} className="px-5 py-10 text-center text-gray-400 text-sm">No recipients in this view.</td></tr>
                ) : (
                  recipients.items.map((r) => (
                    <tr key={r.id} className="hover:bg-indigo-50/30 transition-colors group">
                      <td className="px-5 py-3 text-gray-800 text-xs font-medium group-hover:text-indigo-700 transition-colors">{r.name || "—"}</td>
                      <td className="px-4 py-3 text-gray-500 text-xs">{r.email}</td>
                      <td className="px-4 py-3">
                        <span className={`${BADGE_BASE} ${STATUS_BADGE[r.status] || STATUS_BADGE.draft}`}>
                          {STATUS_ICON[r.status]}{r.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-gray-400 text-xs whitespace-nowrap">{fmtDate(r.sent_at)}</td>
                      <td className="px-5 py-3 text-xs max-w-xs truncate">
                        {r.error_text
                          ? <span className="text-red-500" title={r.error_text}>{r.error_text}</span>
                          : <span className="text-gray-300">—</span>}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {/* Pagination — pinned to bottom of card */}
          {recipients.total > PAGE_SIZE && (
            <div className="flex items-center justify-between px-5 py-3 border-t border-gray-100 flex-shrink-0 bg-white">

              {/* Page info */}
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-400">
                  Showing{" "}
                  <span className="font-semibold text-gray-600">{page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, recipients.total)}</span>
                  {" "}of{" "}
                  <span className="font-semibold text-gray-600">{recipients.total}</span>
                </span>
                {/* Page dots */}
                <div className="hidden sm:flex items-center gap-1 ml-2">
                  {Array.from({ length: Math.ceil(recipients.total / PAGE_SIZE) }).map((_, i) => (
                    <button
                      key={i}
                      onClick={() => setPage(i)}
                      className={`transition-all duration-150 rounded-full cursor-pointer ${
                        i === page
                          ? "w-5 h-2 brand-grad-r"
                          : "w-2 h-2 bg-gray-200 hover:bg-indigo-300"
                      }`}
                    />
                  ))}
                </div>
              </div>

              {/* Prev / Next */}
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={page === 0}
                  className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-semibold border transition-all cursor-pointer disabled:cursor-not-allowed disabled:opacity-40
                    bg-white border-gray-200 text-gray-600 hover:bg-indigo-50 hover:border-indigo-200 hover:text-indigo-600"
                >
                  <ChevronDown size={12} className="rotate-90" /> Previous
                </button>
                <button
                  onClick={() => setPage((p) => p + 1)}
                  disabled={(page + 1) * PAGE_SIZE >= recipients.total}
                  className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-semibold border transition-all cursor-pointer disabled:cursor-not-allowed disabled:opacity-40
                    brand-grad border-transparent text-white hover:opacity-90 shadow-sm"
                >
                  Next <ChevronDown size={12} className="-rotate-90" />
                </button>
              </div>
            </div>
          )}
        </div>

        </div>{/* end outer white container */}
      </div>
    </div>
  );
}
