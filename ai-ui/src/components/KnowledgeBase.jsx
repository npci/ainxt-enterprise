// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from "react";
import { BookOpen, CheckCircle2, ChevronDown, Circle, FileText, History, Loader2, MessageSquare, ShieldOff, Trash2, Upload, X, Lock, Globe } from "lucide-react";
import { useFileDrop } from "../hooks/useFileDrop";
import { API_BASE, authFetch } from "../config";
import { toIST, toISTDate } from "../utils/time";
import { useToast, useConfirm } from './ui/DialogProvider.jsx';
import ScopePicker from "./ScopePicker.jsx";
import { validateIdentifier } from "../utils/securityValidation";
import KbChatPanel from "./KbChatPanel.jsx";
import KbChatList  from "./KbChatList.jsx";
import KbChat      from "./KbChat.jsx";
import { useDeletionHistory, KbDeletionList, KbDeletionDetail } from "./KbDeletionHistory.jsx";

// ── Searchable multi-select dropdown (same as ProductManager) ──
function MultiSelectDept({ options, selected, onChange }) {
  const [open, setOpen]     = useState(false);
  const [search, setSearch] = useState("");
  const ref                 = useRef(null);

  useEffect(() => {
    function handle(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false); }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, []);

  const filtered = options.filter(o => o && o.toLowerCase().includes(search.toLowerCase()));

  function toggle(dept) {
    onChange(selected.includes(dept) ? selected.filter(d => d !== dept) : [...selected, dept]);
  }

  function remove(dept, e) {
    e.stopPropagation();
    onChange(selected.filter(d => d !== dept));
  }

  return (
    <div ref={ref} className="relative">
      <div
        onClick={() => setOpen(o => !o)}
        className="min-h-[38px] w-full bg-white border border-gray-300 rounded px-2 py-1.5 flex flex-wrap gap-1 cursor-pointer focus:border-indigo-600"
      >
        {selected.length === 0 && (
          <span className="text-gray-400 text-sm self-center">Select departments…</span>
        )}
        {selected.map(d => (
          <span key={d} className="flex items-center gap-1 brand-grad hover:opacity-70 text-white text-xs px-2 py-0.5 rounded-full">
            {d}
            <button type="button" onClick={e => remove(d, e)} className="hover:text-blue-600"><X size={10} /></button>
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
              className="w-full text-sm border border-gray-200 rounded px-2 py-1 outline-none shadow-sm focus:border-indigo-300"
            />
          </div>
          <div className="max-h-52 overflow-y-auto">
            {filtered.length === 0 && (
              <div className="px-3 py-2 text-xs text-gray-400">No departments found</div>
            )}
            {filtered.map(dept => (
              <label key={dept} className="flex items-center gap-2 px-3 py-2 text-sm hover:bg-gray-50 cursor-pointer">
                <input type="checkbox" checked={selected.includes(dept)} onChange={() => toggle(dept)} className="accent-indigo-700" />
                {dept}
              </label>
            ))}
          </div>
          {selected.length > 0 && (
            <div className="px-3 py-1.5 border-t border-gray-100 text-xs text-gray-400">{selected.length} selected</div>
          )}
        </div>
      )}
    </div>
  );
}

const SUPPORTED_TYPES   = ["PDF", "DOCX", "MD", "PPTX", "HTML", "TXT"];
const ALLOWED_EXTENSIONS   = SUPPORTED_TYPES.map(t => `.${t.toLowerCase()}`); // → [".pdf", ".docx", ".md", ".ppt", ".pptx", ".html", ".txt"]


// Pipeline stages shown during upload
const STAGES = [
  { key: "parse", label: "Parsing document",         detail: "Extracting text content"        },
  { key: "chunk", label: "Creating chunks",           detail: "Splitting into searchable pieces"},
  { key: "embed", label: "Embedding with AI",         detail: "Generating vector embeddings"   },
  { key: "save",  label: "Saving to knowledge base",  detail: "Persisting to pgvector + DB"    },
];
const STAGE_ORDER = STAGES.map(s => s.key);

// How long to spend on each fast stage before auto-advancing (ms).
// "embed" stays until the API responds — it is genuinely the slow step.
const STAGE_TIMERS = { parse: 700, chunk: 900 };

function fmtSize(bytes) {
  if (!bytes) return "";
  if (bytes < 1024)        return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function fmtDate(iso) { return toIST(iso ? new Date(iso) : null); }

// ── Stage progress card shown during / after upload ──────────────────────────
function UploadProgress({ file, stage, result, onDone }) {
  if (!file) return null;   // guard against stale timer firing after clearUpload
  const currentIdx = STAGE_ORDER.indexOf(stage);
  const isDone     = stage === "done";

  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      {/* Header */}
      <div className={`px-4 py-3 flex items-center gap-3 ${isDone ? "bg-green-50" : "bg-gray-50"}`}>
        {isDone ? (
          <CheckCircle2 size={16} className="text-green-500 flex-shrink-0" />
        ) : (
          <Loader2 size={16} className="text-gray-400 animate-spin flex-shrink-0" />
        )}
        <div className="min-w-0 flex-1">
          <div className="text-xs font-medium text-gray-800 truncate">{file.name}</div>
          <div className="text-[10px] text-gray-400">
            {fmtSize(file.size)}
            {isDone && result && (
              <>
                {result.status === "PENDING_APPROVAL" ? (
                  <span className="text-blue-600 ml-2 font-medium">
                    ✓ {result.chunk_count} chunks ready · awaiting approval to embed
                  </span>
                ) : (
                  <span className="text-green-600 ml-2 font-medium">
                    ✓ {result.chunk_count} chunks embedded
                  </span>
                )}
                {result.duplicate && (
                  <span className="text-yellow-600 ml-2 font-medium">· duplicate detected (skipped re-embed)</span>
                )}
              </>
            )}
          </div>
        </div>
        {isDone && (
          <button
            onClick={onDone}
            className="text-gray-400 hover:text-gray-600 cursor-pointer flex-shrink-0"
          >
            <X size={13} />
          </button>
        )}
      </div>

      {/* Stage steps */}
      <div className="px-4 py-3 space-y-2.5">
        {STAGES.map((s, i) => {
          const stageIdx  = STAGE_ORDER.indexOf(s.key);
          const isActive  = !isDone && s.key === stage;
          const isDoneStep = isDone || stageIdx < currentIdx;
          const isPending = !isDone && stageIdx > currentIdx;

          return (
            <div key={s.key} className="flex items-center gap-3">
              {/* Icon */}
              <div className="flex-shrink-0 w-4 flex items-center justify-center">
                {isDoneStep ? (
                  <CheckCircle2 size={14} className="text-green-500" />
                ) : isActive ? (
                  <Loader2 size={14} className="text-blue-500 animate-spin" />
                ) : (
                  <Circle size={14} className="text-gray-200" />
                )}
              </div>

              {/* Label */}
              <div className="flex-1 min-w-0">
                <span className={`text-xs ${
                  isDoneStep  ? "text-gray-400 line-through" :
                  isActive    ? "text-gray-800 font-medium"  :
                                "text-gray-300"
                }`}>
                  {s.label}
                </span>
                {isActive && (
                  <div className="text-[10px] text-blue-400 mt-0.5">{s.detail}</div>
                )}
              </div>

              {/* Right badge */}
              {isActive && (
                <span className="text-[10px] text-blue-400 flex-shrink-0">
                  {s.key === "embed" ? "may take a moment…" : ""}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────
export default function KnowledgeBase({
  user,
  // App-level chat state — used by the KB Chat tab so KB chats live in
  // the same array as normal chats (filtered locally via isKbChat).
  chats         = [],
  setChats,
  chatsLoading  = false,
  // setActiveChatId is no longer used directly (KB has its own
  // kbActiveChatId so toggling tabs doesn't disturb the main Chat
  // page's active selection) but kept for back-compat.
  setActiveChatId: _setActiveChatId,
}) {
  void _setActiveChatId;
  const { toast }   = useToast();
  const { confirm } = useConfirm();
  const [docs, setDocs]           = useState([]);
  const [namespaces, setNamespaces] = useState([]);
  const [activeNs, setActiveNs]   = useState(null);
  const [activeTab, setActiveTab] = useState("docs"); // docs | inbox
  // Page-level tab: "chat" (default) or "upload".
  // The Chat surface replaces the legacy Generic|Knowledge Base toggle
  // that used to live inside Chat.jsx — KB chats now start from here.
  const [rightMode, setRightMode] = useState("chat");
  // Shared state for the Deleted Docs tab — lifted here so KbDeletionList
  // (left rail) and KbDeletionDetail (right panel) stay in sync without
  // prop-drilling through an intermediate wrapper.
  const deletionState = useDeletionHistory();
  // Active KB chat within this page. null → drill-down picker visible.
  // Kept separate from App.activeChatId so the main Chat page's
  // selection isn't perturbed when the user is browsing KB chats.
  const [kbActiveChatId, setKbActiveChatId] = useState(null);
  const [selectedDepts, setSelectedDepts] = useState([]);  // [] = org-wide (all departments)
  const [availableDepts, setAvailableDepts] = useState([]);
  const [pendingDocs, setPendingDocs]   = useState([]);
  const [loading, setLoading]     = useState(true);
  const [error, setError]         = useState(null);
  const [searchQ, setSearchQ]     = useState("");

  const isAdmin         = user?.role === "admin";
  // isC1Plus: driven by can_approve from backend (uses APPROVAL_AD_LEVEL config)
  const isC1Plus        = isAdmin || (user?.can_approve === true);
  const userDept        = user?.department || "";
  // canSelectAnyDept: very senior access (L0-L1) — kept as raw ad_level check
  // since this is a stricter gate than approval (C-suite / director only)
  const canSelectAnyDept = isAdmin || ((user?.ad_level ?? 6) < 2);
  const [visibility, setVisibility] = useState("PUBLIC");

  // Phase 1 — spec scope metadata state
  const [specProductId,    setSpecProductId]    = useState("");
  const [specDomain,       setSpecDomain]       = useState("");
  const [specVersion,      setSpecVersion]      = useState("");
  const [specVersionDate,  setSpecVersionDate]  = useState("");
  const [deprecatePrior,   setDeprecatePrior]   = useState(false);
  // ── Part U13 (2026-06-08) — docx §8 source_type dropdown ──
  // Captures doc kind at upload so retrieval can filter by type and the
  // citation footer can render a typed badge. "" = OTHER (server-side
  // normalisation). CHECK enum: BRD / FSD / TPMC_DECISION / RBI_CIRCULAR /
  // ARCHITECTURE / SPEC / OTHER.
  const [sourceType,       setSourceType]       = useState("");
  // Note: product list is fetched inside <ScopePicker/> — same /products
  // endpoint, same shape — so we don't duplicate the request here.

  // Upload progress state
  const [uploadFile,  setUploadFile]  = useState(null);   // { name, size }
  const [uploadStage, setUploadStage] = useState(null);   // null | parse|chunk|embed|save|done
  const [uploadResult, setUploadResult] = useState(null); // { chunk_count }
  // Compliance block state — only populated when COMPLIANCE_SCAN_KB_UPLOAD=true on the server
  // and the backend returns blocked:true. When the flag is off, this stays null always.
  const [complianceBlock, setComplianceBlock] = useState(null);
  // Warn-on-attempt flag: only show the "No Domain and Product set" banner
  // once the user has actually attempted to upload without those mandatory
  // scope fields. Cleared automatically when both fields become set.
  const [scopeWarn, setScopeWarn] = useState(false);
  // approvingId removed — approvals now handled via Inbox only

  const fileInputRef   = useRef(null);
  const stageTimerRef  = useRef(null);   // embed-stage timer
  const chunkTimerRef  = useRef(null);   // chunk-stage timer (tracked separately)

  const ACCEPTED_MIME_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/markdown",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/html",
    "text/plain",
  ];

  const { isDragging, dropRef } = useFileDrop({
    accept: ACCEPTED_MIME_TYPES,
    disabled: false,
    onFiles: (validFiles, invalidFiles) => {
      // BUG-A FIX: MIME types are unreliable across OS/browsers:
      //   .md  → often reported as "text/plain" instead of "text/markdown"
      //   .ppt → often reported as "application/octet-stream"
      //   .html → sometimes "application/xhtml+xml"
      // Re-validate "invalid" files by extension as a fallback so these types
      // are not silently rejected at the drag-and-drop layer.
      const ALLOWED_EXTS = ["pdf", "docx", "md", "ppt", "pptx", "html", "txt"];
      const revalidatedByExt = (invalidFiles || []).filter(f => {
        const ext = f.name.split(".").pop().toLowerCase();
        return ALLOWED_EXTS.includes(ext);
      });
      const trulyInvalid = (invalidFiles || []).filter(f => {
        const ext = f.name.split(".").pop().toLowerCase();
        return !ALLOWED_EXTS.includes(ext);
      });
      const allValid = [...validFiles, ...revalidatedByExt];

      if ((allValid.length + trulyInvalid.length) > 1) {
        setError("Only one file can be uploaded at a time. Please drop a single file.");
        return;
      }
      if (trulyInvalid.length > 0) {
        const invalidNames = trulyInvalid.map(f => f.name).join(", ");
        setError(
          `Unsupported file type. Only PDF, DOCX, MD, PPTX, HTML, and TXT files are allowed. ` +
          `Skipped: ${invalidNames}`
        );
        if (allValid.length === 0) return;
      }
      if (allValid.length > 0) {
        // BUG-B FIX: show explicit error instead of silently ignoring the drop
        const deptReady = !canSelectAnyDept || visibility !== "PRIVATE" || selectedDepts.length > 0;
        if (!deptReady) {
          setError("Please select a department before uploading.");
          return;
        }
        handleUpload(allValid);
      }
    },
  });

  useEffect(() => {
    fetchDocs();
    fetchNamespaces();
    fetchPendingDocs();   // all users: non-approvers see their own pending; approvers see inbox
    // Phase 1 — product list now loaded inside <ScopePicker/>.
    if (isC1Plus) {
      fetchDepartments();
    } else if (userDept) {
      // Non-approvers: lock department to their own — no dropdown needed
      setSelectedDepts([userDept]);
    }
  }, []);

  useEffect(() => { fetchDocs(); }, [activeNs]);

  // Auto-dismiss the scope warning the moment all three mandatory fields are set.
  useEffect(() => {
    if (specDomain && specProductId && specVersion && scopeWarn) setScopeWarn(false);
  }, [specDomain, specProductId, specVersion, scopeWarn]);

  // Clean up any pending stage timers on unmount
  useEffect(() => () => {
    clearTimeout(stageTimerRef.current);
    clearTimeout(chunkTimerRef.current);
  }, []);

  // ── Poll for INDEXING docs in the Documents tab every 5 s ───────────────────
  // When any document in the main docs list is INDEXING, poll every 5 s.
  // Once status changes (ACTIVE or rollback to PENDING_APPROVAL), refresh the
  // full docs list so the badge updates without a manual page reload.
  useEffect(() => {
    const indexingDocs = (docs || []).filter(d => d.status === "INDEXING");
    if (indexingDocs.length === 0) return;

    const interval = setInterval(async () => {
      let anyStillIndexing = false;
      for (const d of indexingDocs) {
        try {
          const r = await authFetch(`${API_BASE}/kb/${d.id}`);
          if (r.ok) {
            const updated = await r.json();
            if (updated.status !== "INDEXING") {
              // Status changed — refresh full docs list and stop polling
              fetchDocs();
              return;
            } else {
              anyStillIndexing = true;
            }
          }
        } catch (_err) {
          // Network error — keep polling silently
        }
      }
      if (!anyStillIndexing) clearInterval(interval);
    }, 5000);

    return () => clearInterval(interval);
  }, [docs]);

  // ── Poll for INDEXING docs in the Request Status tab every 5 s ───────────────
  // The uploader sees their INDEXING docs in the Request Status tab (not Inbox).
  // Poll every 5 s so the ⏳ badge auto-updates to ✅ once kb_worker finishes,
  // without the uploader needing to manually refresh the page.
  useEffect(() => {
    const indexingPending = (pendingDocs || []).filter(d => d.status === "INDEXING");
    if (indexingPending.length === 0) return;

    const interval = setInterval(async () => {
      let anyStillIndexing = false;
      for (const d of indexingPending) {
        try {
          const r = await authFetch(`${API_BASE}/kb/${d.id}`);
          if (r.ok) {
            const updated = await r.json();
            if (updated.status !== "INDEXING") {
              // Status changed — refresh both lists so both tabs update together
              fetchDocs();
              fetchPendingDocs();
              return;
            } else {
              anyStillIndexing = true;
            }
          }
        } catch (_err) {
          // Network error — keep polling silently
        }
      }
      if (!anyStillIndexing) clearInterval(interval);
    }, 5000);

    return () => clearInterval(interval);
  }, [pendingDocs]);

  // Poller 3 — watch PENDING_APPROVAL docs so the UI auto-updates when an
  // admin approves a document (PENDING_APPROVAL → INDEXING → ACTIVE) without
  // the uploader needing to manually switch tabs or refresh the page.
  useEffect(() => {
    const waitingDocs = (pendingDocs || []).filter(d => d.status === "PENDING_APPROVAL");
    if (waitingDocs.length === 0) return;

    const interval = setInterval(async () => {
      for (const d of waitingDocs) {
        try {
          const r = await authFetch(`${API_BASE}/kb/${d.id}`);
          if (r.ok) {
            const updated = await r.json();
            if (updated.status !== "PENDING_APPROVAL") {
              // Doc was approved (now INDEXING) or rejected — refresh both lists
              fetchDocs();
              fetchPendingDocs();
              return;
            }
          }
        } catch (_err) {
          // Network error — keep polling silently
        }
      }
    }, 8000); // 8s — approval is a human action, no need to be aggressive

    return () => clearInterval(interval);
  }, [pendingDocs]);


  async function fetchDocs() {
    setLoading(true);
    try {
      const url  = activeNs
        ? `${API_BASE}/kb?namespace=${encodeURIComponent(activeNs)}&limit=10000`
        : `${API_BASE}/kb?limit=10000`;
      const res  = await authFetch(url);
      const data = await res.json();
      setDocs(data.docs || []);
    } catch {
      setError("Failed to load documents");
    } finally {
      setLoading(false);
    }
  }

  async function fetchNamespaces() {
    try {
      const res  = await authFetch(`${API_BASE}/kb/namespaces`);
      const data = await res.json();
      setNamespaces(data.namespaces || []);
    } catch {}
  }

  async function fetchDepartments() {
    try {
      const res  = await authFetch(`${API_BASE}/products/departments`);
      const data = await res.json();
      setAvailableDepts((data.departments || []).filter(d => d && d.trim() !== ""));
    } catch {}
  }

  // Phase 1 — fetch product list for spec scope selector
  // fetchProducts removed — <ScopePicker/> owns the /products call now.

  async function fetchPendingDocs() {
    try {
      // Fetch PENDING_APPROVAL, REJECTED, and INDEXING docs in parallel.
      // INDEXING = approved but kb_worker is still parsing (not yet searchable).
      // Uploaders need to see INDEXING in their Request Status tab so they know
      // their doc is being processed — the Inbox screen is approver-only for actions.
      const [pendingRes, rejectedRes, indexingRes] = await Promise.all([
        authFetch(`${API_BASE}/kb?status=PENDING_APPROVAL&limit=100`),
        authFetch(`${API_BASE}/kb?status=REJECTED&limit=100`),
        authFetch(`${API_BASE}/kb?status=INDEXING&limit=100`),
      ]);
      const pendingData  = pendingRes.ok   ? await pendingRes.json()   : { docs: [] };
      const rejectedData = rejectedRes.ok  ? await rejectedRes.json()  : { docs: [] };
      const indexingData = indexingRes.ok  ? await indexingRes.json()  : { docs: [] };
      const allDocs = [
        ...(pendingData.docs  || []),
        ...(rejectedData.docs || []),
        ...(indexingData.docs || []),
      ];
      // Non-approvers only see their own docs in Request Status
      const _userEmail = user?.email || "";
      const filtered = isC1Plus
        ? allDocs
        : allDocs.filter(d => !_userEmail || d.uploaded_by === _userEmail);
      setPendingDocs(filtered);
    } catch (e) {
      console.error("fetchPendingDocs failed:", e);
    }
  }

  // approveDoc and rejectDoc removed — approvals handled via Inbox only

  async function handleUpload(files) {
    if (!files || files.length === 0) return;
    if (files.length > 1) { setError("Only one file can be uploaded at a time. Please select a single file."); return; }
    setError(null);
    // Mandatory scope gate — Department + Product + Spec Version are required
    // for the doc to be reachable from product-pinned chats. Surface the
    // warning banner only when the user actually attempts an upload without
    // them.
    if (!specDomain || !specProductId || !specVersion) {
      setScopeWarn(true);
      return;
    }
    setScopeWarn(false);
    const ns = specDomain.trim();
    if (!ns) { setError("Please select a domain before uploading."); return; }

    // Client-side pre-check — mirrors validate_docs_upload_scope() in
    // core/security_validation.py (identifier allow-list for domain/
    // spec_version). The backend (POST /kb/upload) remains the
    // authoritative enforcer.
    const domainCheck = validateIdentifier(ns);
    if (!domainCheck.isValid) { setError(domainCheck.errors[0]?.message || "Invalid domain"); return; }
    if (specVersion) {
      const specCheck = validateIdentifier(specVersion);
      if (!specCheck.isValid) { setError(specCheck.errors[0]?.message || "Invalid spec version"); return; }
    }

    for (const file of files) {
      // ── 0. Client-side size check — instant feedback, no upload wasted ──
      if (file.size > 25 * 1024 * 1024) {
        setError(`File "${file.name}" is too large (${fmtSize(file.size)}). Maximum allowed size is 25 MB.`);
        return;
      }
      // ── 1. Show "parse" stage immediately ──────────────────────────
      setUploadFile({ name: file.name, size: file.size });
      setUploadStage("parse");
      setUploadResult(null);

      // ── 2. Advance fast stages on timers ────────────────────────────
      // Use separate refs so BOTH timers can be cancelled when the API responds.
      chunkTimerRef.current = setTimeout(() => setUploadStage("chunk"), STAGE_TIMERS.parse);
      stageTimerRef.current = setTimeout(
        () => setUploadStage("embed"),
        STAGE_TIMERS.parse + STAGE_TIMERS.chunk,
      );

      // ── 3. Call API (embedding is the real bottleneck) ───────────────
      try {
        const form = new FormData();
        form.append("namespace", ns);
        form.append("files", file);
        form.append("visibility", visibility);
        form.append("department_ids", JSON.stringify(selectedDepts));
        // Phase 1 — spec scope metadata
        if (specProductId)   form.append("product_id",      specProductId);
        if (specDomain)      form.append("domain",          specDomain);
        if (specVersion)     form.append("spec_version",    specVersion);
        if (specVersionDate) form.append("version_date",    specVersionDate);
        form.append("deprecate_prior", deprecatePrior ? "true" : "false");
        // Part U13 (docx §8) — doc kind dropdown
        if (sourceType)      form.append("source_type",     sourceType);
        const res = await authFetch(`${API_BASE}/kb/upload`, { method: "POST", body: form });
        if (res.status === 413) throw new Error("File is too large. Maximum allowed size is 25 MB.");
        if (res.status === 415) {
          let detail = "Unsupported file type. Only PDF, DOCX, MD, PPTX, HTML, and TXT files are allowed.";
          try { const errData = await res.json(); detail = errData.detail || detail; } catch {}
          throw new Error(detail);
        }
        if (!res.headers.get("content-type")?.includes("application/json"))
          throw new Error(`Upload failed (HTTP ${res.status})`);
        const data = await res.json();
        // Compliance block — only fires when COMPLIANCE_SCAN_KB_UPLOAD=true on the server.
        // When the flag is off the backend never returns blocked:true, so this is a no-op.
        if (data.blocked) {
          clearTimeout(stageTimerRef.current);
          clearTimeout(chunkTimerRef.current);
          setUploadStage(null);
          setUploadFile(null);
          setComplianceBlock({
            filename:           data.filename || file.name,
            block_reason:       data.block_reason || "PCI/PII data",
            compliance_reasons: data.compliance_reasons || [],
          });
          continue;
        }
        if (!data.success) throw new Error(data.error || data.detail || "Upload failed");

        // ── 4b. Flash "save" then "done" ─────────────────────────────────
        clearTimeout(stageTimerRef.current);
        clearTimeout(chunkTimerRef.current);
        setUploadStage("save");
        setUploadResult({
          chunk_count: data.chunk_count,
          duplicate: data.duplicate,
          status: data.status,
        });

        await new Promise(r => setTimeout(r, 600));
        setUploadStage("done");

        // Refresh lists in background
        fetchDocs();
        fetchNamespaces();
        fetchPendingDocs();

      } catch (e) {
        clearTimeout(stageTimerRef.current);
        clearTimeout(chunkTimerRef.current);
        setUploadStage(null);
        setUploadFile(null);
        setError(e.message || "Upload failed");
      }
    }
  }

  function clearUpload() {
    clearTimeout(stageTimerRef.current);
    clearTimeout(chunkTimerRef.current);
    setUploadStage(null);
    setUploadFile(null);
    setUploadResult(null);
    setComplianceBlock(null);
  }

  async function handleDelete(docId) {
    const ok = await confirm({ title: "Delete Document", message: "Delete this document and all its embeddings? This cannot be undone.", confirmLabel: "Delete" });
    if (!ok) return;
    try {
      await authFetch(`${API_BASE}/kb/${docId}`, { method: "DELETE" });
      setDocs(prev => prev.filter(d => d.id !== docId));
      setPendingDocs(prev => prev.filter(d => d.id !== docId));
    } catch {
      setError("Delete failed");
    }
  }

  const currentUserEmail = user?.email || "";

  // Documents tab: only approved/active docs. Pending and rejected docs live in
  // "Request Status" tab exclusively — never mix them into the Documents list.
  const filteredDocs = docs?.filter(d => {
    if (d.status === "PENDING_APPROVAL" || d.status === "REJECTED") return false;
    if (activeNs && d.namespace !== activeNs) return false;
    if (searchQ && ![d.name, d.filename, d.namespace].some(f => f?.toLowerCase().includes(searchQ.toLowerCase()))) return false;
    return true;
  });
  const filteredPendingDocs = pendingDocs.filter(doc => {
    if (
      searchQ &&
      ![doc.name, doc.filename, doc.namespace]
        .some(f => f?.toLowerCase().includes(searchQ.toLowerCase()))
    ) return false;

    return true;
  });
  const approvedDocs = docs?.filter(d => d.status !== "PENDING_APPROVAL" && d.status !== "REJECTED") || [];
  const totalChunks = approvedDocs.reduce((s, d) => s + (d.chunk_count || 0), 0);
  const isUploading = uploadStage !== null && uploadStage !== "done";

  return (
    <div className="flex flex-col h-full overflow-hidden bg-white">

      {/* ── PAGE TITLE BAR — shared between Chat and Upload tabs ──
          One source of truth for the "Knowledge Base" label; the duplicate
          headers that used to live inside the Upload-tab docs panel and
          the KbChatList header are removed so the chrome doesn't repeat. */}
      <div className="flex items-center gap-2 px-6 py-3 border-b border-gray-200 flex-shrink-0 bg-white">
        <BookOpen size={16} className="text-indigo-700" />
        <span className="text-sm font-semibold text-indigo-700">Knowledge Base</span>
      </div>

      {/* ── PAGE BODY ──
          The Chat|Upload tab strip is rendered INSIDE the left rail
          (sized w-72) so the right pane keeps its full vertical height —
          the tab strip no longer steals a horizontal slice from the
          right pane. */}
      <div className="flex flex-1 overflow-hidden">

      {/* Shared tab strip used by both branches. Sized to the left rail. */}
      {(() => null)() /* placeholder — strip is inlined per-branch below to keep within the same w-72 column */}

      {rightMode === "chat" ? (
        // ── CHAT TAB — KbChatList (left) + drill-down OR KbChat (right)
        <>
          <div className="w-72 flex-shrink-0 flex flex-col overflow-hidden bg-gray-50/40 border-r border-gray-200">
            {/* Tab strip lives inside the left rail so it doesn't span
                the full page width. */}
            <div className="flex items-center px-2 pt-2 gap-0.5 flex-shrink-0 bg-white border-b border-gray-200">
              {[
                { key: "chat",    label: "Chat",    icon: MessageSquare },
                { key: "upload",  label: "Upload",  icon: Upload },
                { key: "deleted", label: "Deleted Docs", icon: History },
              ].map(m => (
                <button
                  key={m.key}
                  type="button"
                  onClick={() => setRightMode(m.key)}
                  className={`flex items-center gap-1 px-2 py-2 text-xs font-medium transition cursor-pointer ${
                    rightMode === m.key
                      ? "border-b-2 border-indigo-600 text-indigo-700"
                      : "text-gray-400 hover:text-gray-600"
                  }`}
                >
                  <m.icon size={12} />
                  {m.label}
                </button>
              ))}
            </div>
            <div className="flex-1 overflow-hidden">
              <KbChatList
                chats={chats}
                setChats={setChats}
                activeChatId={kbActiveChatId}
                setActiveChatId={setKbActiveChatId}
                chatsLoading={chatsLoading}
                onNewChat={() => setKbActiveChatId(null)}
                pickerVisible={!kbActiveChatId}
              />
            </div>
          </div>
          <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
            {kbActiveChatId ? (
              <KbChat
                chats={chats}
                setChats={setChats}
                activeChatId={kbActiveChatId}
                setActiveChatId={setKbActiveChatId}
                user={user}
                chatsLoading={chatsLoading}
              />
            ) : (
              <KbChatPanel
                user={user}
                onHandoff={(chatObj) => {
                  // Add the new KB chat to App state and switch the right
                  // pane to the KbChat surface. NO navigation — the user
                  // stays inside the Knowledge Base section.
                  if (typeof setChats === "function") {
                    setChats(prev => {
                      const existing = Array.isArray(prev) ? prev : [];
                      if (existing.some(c => c.id === chatObj.id)) return existing;
                      return [chatObj, ...existing];
                    });
                  }
                  setKbActiveChatId(chatObj.id);
                }}
              />
            )}
          </div>
        </>
      ) : rightMode === "deleted" ? (
      // ── DELETED DOCS TAB — left rail (tab strip + summary list) + right detail panel ──
      <>
      <div className="w-72 bg-gray-50 border-r border-gray-200 flex flex-col overflow-hidden flex-shrink-0">
        {/* Tab strip — same as Chat and Upload tabs */}
        <div className="flex items-center px-2 pt-2 gap-0.5 flex-shrink-0 bg-white border-b border-gray-200">
          {[
            { key: "chat",    label: "Chat",         icon: MessageSquare },
            { key: "upload",  label: "Upload",        icon: Upload },
            { key: "deleted", label: "Deleted Docs",  icon: History },
          ].map(m => (
            <button
              key={m.key}
              type="button"
              onClick={() => setRightMode(m.key)}
              className={`flex items-center gap-1 px-2 py-2 text-xs font-medium transition cursor-pointer ${
                rightMode === m.key
                  ? "border-b-2 border-indigo-600 text-indigo-700"
                  : "text-gray-400 hover:text-gray-600"
              }`}
            >
              <m.icon size={12} />
              {m.label}
            </button>
          ))}
        </div>
        {/* Summary list — fills the rest of the left rail */}
        <KbDeletionList state={deletionState} />
      </div>
      {/* Detail panel — fills the remaining width */}
      <div className="flex-1 flex flex-col overflow-hidden bg-white">
        <KbDeletionDetail row={deletionState.selectedRow} />
      </div>
      </>
      ) : (
      // ── UPLOAD TAB — original docs list (left) + upload form (right) ──
      <>
      {/* ── LEFT PANEL ── */}
      <div className="w-72 bg-gray-50 border-r border-gray-200 flex flex-col overflow-hidden flex-shrink-0">

        {/* Tab strip lives inside the left rail so it doesn't span the
            full page width. */}
        <div className="flex items-center px-2 pt-2 gap-0.5 flex-shrink-0 bg-white border-b border-gray-200">
          {[
            { key: "chat",    label: "Chat",    icon: MessageSquare },
            { key: "upload",  label: "Upload",  icon: Upload },
            { key: "deleted", label: "Deleted Docs", icon: History },
          ].map(m => (
            <button
              key={m.key}
              type="button"
              onClick={() => setRightMode(m.key)}
              className={`flex items-center gap-1 px-2 py-2 text-xs font-medium transition cursor-pointer ${
                rightMode === m.key
                  ? "border-b-2 border-indigo-600 text-indigo-700"
                  : "text-gray-400 hover:text-gray-600"
              }`}
            >
              <m.icon size={12} />
              {m.label}
            </button>
          ))}
        </div>


        {/* Doc-count summary — the duplicate "Knowledge Base" header here
            was removed in favour of the shared page-title bar above the
            tab strip. Keep only the per-tab count summary. */}
        <div className="px-4 py-2.5 border-b border-gray-200 text-xs text-gray-400">
          {activeTab === "inbox"
            ? `${pendingDocs.filter(d => d.status === "PENDING_APPROVAL" && d.uploaded_by === currentUserEmail).length} pending request${pendingDocs.filter(d => d.status === "PENDING_APPROVAL" && d.uploaded_by === currentUserEmail).length !== 1 ? "s" : ""}`
            : `${approvedDocs.length} doc${approvedDocs.length !== 1 ? "s" : ""} · ${totalChunks} chunks embedded`
          }
        </div>

        <div className="flex border-b border-gray-200">
          <button
            onClick={() => setActiveTab("docs")}
            className={`flex-1 px-2 py-2 text-xs font-medium cursor-pointer ${activeTab === "docs" ? "border-b-2 border-indigo-600 bg-gradient-to-br from-indigo-600 to-violet-600 bg-clip-text text-transparent" : "text-gray-400 hover:text-gray-600"}`}
          >
            Documents
          </button>
          <button
            onClick={() => { setActiveTab("inbox"); fetchPendingDocs(); }}
            className={`flex-1 px-2 py-2 text-xs font-medium relative cursor-pointer ${activeTab === "inbox" ? "border-b-2 border-indigo-600 bg-gradient-to-br from-indigo-600 to-violet-600 bg-clip-text text-transparent" : "text-gray-400 hover:text-gray-600"}`}
          >
            Request Status
            {pendingDocs.filter(d => d.status === "PENDING_APPROVAL" || d.status === "INDEXING").length > 0 && (
              <span className="absolute top-1.5 right-1 w-4 h-4 flex items-center justify-center bg-amber-400 text-white text-[9px] rounded-full font-bold">
                {pendingDocs.filter(d => d.status === "PENDING_APPROVAL" || d.status === "INDEXING").length}
              </span>
            )}
          </button>
        </div>

        <div className="px-3 py-2 border-b border-gray-100">
          <input
            value={searchQ}
            onChange={e => setSearchQ(e.target.value)}
            placeholder="Search documents..."
            className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-md outline-none bg-white focus:border-indigo-300 shadow-sm"
          />
          {/* Active namespace filter indicator */}
          {activeNs && (
            <div className="mt-1.5 flex items-center gap-1.5">
              <span className="text-[10px] text-gray-400">Filtered by:</span>
              <span className="flex items-center gap-1 brand-grad hover:opacity-70 text-white text-[10px] px-2 py-0.5 rounded-full cursor-pointer">
                {activeNs}
                <button onClick={() => setActiveNs(null)} className="cursor-pointer">
                  <X size={9} />
                </button>
              </span>
            </div>
          )}
        </div>

        <div className="flex-1 overflow-y-auto">
          {activeTab === "inbox" ? (
            filteredPendingDocs.length === 0 ? (
              <div className="p-4 text-xs text-gray-400 text-center">
                No requests yet
              </div>
            ) : (
              filteredPendingDocs.map(doc => (
                <div key={doc.id} className="px-4 py-3 border-b border-gray-100 hover:bg-gray-100 group rounded m-1">
                  <div className="flex items-start gap-2">
                    <FileText size={13} className="text-gray-400 mt-0.5 flex-shrink-0" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <div className="text-sm font-medium text-gray-600 truncate" title={doc.name || doc.filename}>{doc.name || doc.filename}</div>
                        {doc.status === "PENDING_APPROVAL" && doc.uploaded_by === currentUserEmail && (
                          <button
                            onClick={() => handleDelete(doc.id)}
                            className="opacity-0 group-hover:opacity-100 p-1 hover:bg-red-100 rounded text-red-500 transition flex-shrink-0 cursor-pointer"
                            title="Retract document"
                          >
                            <Trash2 size={12} />
                          </button>
                        )}
                      </div>
                      <div className="text-xs text-gray-400">{doc.namespace} · {fmtSize(doc.file_size)}</div>
                      {doc.uploaded_by && (
                        <div className="text-xs text-gray-400 mt-0.5">
                          Submitted by <span className="font-medium text-gray-600">{doc.uploaded_by}</span>
                          {doc.created_at && <> · {fmtDate(doc.created_at)}</>}
                        </div>
                      )}
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {doc.status === "REJECTED" ? (
                          /* Rejected — show red badge */
                          <span className="bg-red-50 text-red-600 border border-red-200 text-[10px] px-1.5 py-0.5 rounded font-medium">
                            Rejected
                          </span>
                        ) : doc.status === "INDEXING" ? (
                          /* INDEXING — kb_worker is parsing, not yet searchable */
                          <span className="inline-flex items-center gap-1 bg-blue-50 text-blue-600 border border-blue-200 text-[10px] px-1.5 py-0.5 rounded font-medium animate-pulse">
                            ⏳ Parsing &amp; indexing — will be searchable shortly
                          </span>
                        ) : doc.status === "ACTIVE" ? (
                          /* ACTIVE — fully indexed and RAG-searchable */
                          <span className="inline-flex items-center gap-1 bg-green-50 text-green-600 border border-green-200 text-[10px] px-1.5 py-0.5 rounded font-medium">
                            ✅ Indexed &amp; searchable
                          </span>
                        ) : (
                          /* PENDING_APPROVAL — awaiting approver action */
                          <span className="bg-yellow-50 text-yellow-600 border border-yellow-200 text-[10px] px-1.5 py-0.5 rounded font-medium">
                            Awaiting approval — action available in Inbox
                          </span>
                        )}
                      </div>
                      {doc.status === "REJECTED" && doc.rejection_reason && (
                        <div className="mt-1 text-xs text-red-600 bg-red-50 border border-red-100 rounded px-2 py-1">
                          <span className="font-medium">Reason:</span> {doc.rejection_reason}
                        </div>
                      )}
                      {doc.parse_error && doc.status === "PENDING_APPROVAL" && (
                        <div className="mt-1.5 text-xs bg-red-50 border border-red-200 rounded px-2 py-1.5">
                          <div className="font-medium text-red-600 mb-0.5">⚠️ Last activation attempt failed</div>
                          <div className="text-red-500 break-words">{doc.parse_error}</div>
                          <div className="text-gray-400 mt-0.5">Re-approve to retry once the issue is resolved.</div>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))
            )
          ) : loading ? (
            <div className="p-4 text-xs text-gray-400 text-center">Loading…</div>
          ) : filteredDocs.length === 0 ? (
            <div className="p-4 text-xs text-gray-400 text-center">
              {docs.length === 0 ? "No documents yet. Upload some on the right." : "No documents match your search."}
            </div>
          ) : (
            filteredDocs.map(doc => (
              <div key={doc.id} className="px-4 py-3 border-b border-gray-100 hover:bg-gray-100 group rounded m-1">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-start gap-2 min-w-0">
                    <FileText size={13} className="text-gray-400 mt-0.5 flex-shrink-0" />
                    <div className="min-w-0">
                      <div className="text-sm font-medium text-gray-600 truncate" title={doc.name}>{doc.name}</div>
                      {doc.uploaded_by && (
                        <div className="text-xs text-gray-400 truncate">{doc.uploaded_by}</div>
                      )}
                      <div className="flex items-center gap-2 mt-1 flex-wrap">
                        <button
                          onClick={e => { e.stopPropagation(); setActiveNs(activeNs === doc.namespace ? null : doc.namespace); }}
                          title={activeNs === doc.namespace ? "Clear filter" : `Filter by ${doc.namespace}`}
                          className={`text-[10px] px-1.5 py-0.5 rounded cursor-pointer transition ${
                            activeNs === doc.namespace
                              ? "brand-grad hover:opacity-70 text-white"
                              : "bg-indigo-50 text-indigo-700 hover:bg-indigo-100"
                          }`}
                        >
                          {doc.namespace}
                        </button>
                        {doc.visibility === "PRIVATE" ? (
                          <span className="flex items-center gap-0.5 bg-gray-100 text-gray-500 text-[10px] px-1.5 py-0.5 rounded">
                            <Lock size={8} /> Private
                          </span>
                        ) : (
                          <span className="flex items-center gap-0.5 bg-green-50 text-green-600 text-[10px] px-1.5 py-0.5 rounded">
                            <Globe size={8} /> Public
                          </span>
                        )}
                        {(doc.department_ids || []).length > 0 ? (
                          (doc.department_ids).map(dept => (
                            <span key={dept} className="bg-indigo-50 text-indigo-600 text-[10px] px-1.5 py-0.5 rounded">
                              {dept}
                            </span>
                          ))
                        ) : (
                          <span className="bg-gray-50 text-gray-400 text-[10px] px-1.5 py-0.5 rounded">
                            All depts
                          </span>
                        )}
                        <span className="text-xs text-gray-400">{doc.chunk_count} chunks</span>
                        <span className="text-xs text-gray-400">{fmtSize(doc.file_size)}</span>
                        {/* INDEXING: pulsing blue badge — Docling parse in progress */}
                        {doc.status === "INDEXING" && (
                          <span className="inline-flex items-center gap-1 bg-blue-50 text-blue-600 border border-blue-200 text-[10px] px-1.5 py-0.5 rounded font-medium animate-pulse">
                            ⏳ Parsing...
                          </span>
                        )}
                        {/* ACTIVE: green badge — fully indexed and RAG-searchable */}
                        {doc.status === "ACTIVE" && (
                          <span className="inline-flex items-center gap-1 bg-green-50 text-green-600 border border-green-200 text-[10px] px-1.5 py-0.5 rounded font-medium">
                            ✅ Searchable
                          </span>
                        )}
                      </div>
                      {doc.parse_error && doc.status === "PENDING_APPROVAL" && (
                        <div className="mt-1 text-xs bg-red-50 border border-red-200 rounded px-2 py-1.5">
                          <div className="font-medium text-red-600 mb-0.5">⚠️ Last activation attempt failed</div>
                          <div className="text-red-500 break-words">{doc.parse_error}</div>
                          <div className="text-gray-400 mt-0.5">Re-approve to retry once the issue is resolved.</div>
                        </div>
                      )}
                      <div className="text-xs text-gray-300 mt-0.5">{fmtDate(doc.created_at)}</div>
                    </div>
                  </div>
                  {(isAdmin || user?.email?.trim() === doc?.uploaded_by?.trim()) && 
                  <button
                    onClick={() => handleDelete(doc.id)}
                    className={`opacity-0 group-hover:opacity-100 p-1 hover:bg-red-100 rounded text-red-500 transition flex-shrink-0 cursor-pointer`}
                    title="Delete document"
                  >
                    <Trash2 size={12} />
                  </button>}
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* ── RIGHT PANEL — Upload form (Chat mode handled at page level above) ── */}
      <div className="flex-1 flex flex-col overflow-hidden bg-white">
        <div className="flex-1 overflow-y-auto p-6">
        <div className="max-w-xl mx-auto w-full space-y-6">

          <div>
            <h2 className="text-base font-semibold text-gray-800">Upload Documents</h2>
            <p className="text-xs text-gray-400 mt-0.5">
              Documents are parsed, chunked, and embedded for RAG retrieval across all chat sessions.
            </p>
          </div>

          {/* Phase 1 — Spec scope metadata (shared ScopePicker — same widget
              used by the Chat screen so upload + chat scope live in one place). */}
          <ScopePicker
            value={{
              product_id:      specProductId,
              domain:          specDomain,
              spec_version:    specVersion,
              version_date:    specVersionDate,
              deprecate_prior: deprecatePrior,
              source_type:     sourceType,
            }}
            onChange={(next) => {
              if (next.product_id      !== specProductId)    setSpecProductId(next.product_id || "");
              if (next.domain          !== specDomain)       setSpecDomain(next.domain || "");
              if (next.spec_version    !== specVersion)      setSpecVersion(next.spec_version || "");
              if (next.version_date    !== specVersionDate)  setSpecVersionDate(next.version_date || "");
              if (!!next.deprecate_prior !== deprecatePrior) setDeprecatePrior(!!next.deprecate_prior);
              if (next.source_type     !== sourceType)       setSourceType(next.source_type || "");
            }}
            includeUploadFields
            disabled={isUploading}
          />

          {/* Phase 1 wiring discoverability — soft warning when the user
              attempts to upload without Domain + Product. Hidden until an
              upload is actually tried so it doesn't nag on first paint.
              Auto-clears as soon as both fields are set. (Audit Fix #8) */}
          {!isUploading && scopeWarn && (!specDomain || !specProductId || !specVersion) && (
            <div className="flex items-start gap-2 px-3 py-2 bg-amber-50 border border-amber-200 rounded text-[11px] text-amber-800">
              <ShieldOff size={12} className="mt-0.5 shrink-0" />
              <span>
                <strong>Domain, Product, and Spec Version are required.</strong>{" "}
                Set all three to make it queryable.
              </span>
            </div>
          )}

          {/* Visibility & department scope */}
          <div className={`space-y-3 ${isUploading ? "opacity-50 pointer-events-none" : ""}`}>
            <div>
              <label className="block text-xs font-medium text-gray-700 mb-1.5">Visibility</label>
              <div className="flex gap-2">
                {["PUBLIC", "PRIVATE"].map(v => (
                  <button
                    key={v}
                    onClick={() => { setVisibility(v); if (v === "PUBLIC") setSelectedDepts([]); else if (canSelectAnyDept) setSelectedDepts([]); else setSelectedDepts([userDept]); }}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium transition cursor-pointer ${
                      visibility === v ? "brand-grad hover:opacity-70 text-white" : "bg-gray-50 text-gray-600 hover:bg-gray-100"
                    }`}
                  >
                    {v === "PRIVATE" ? <Lock size={10} /> : <Globe size={10} />}
                    {v === "PUBLIC" ? "Public — visible to all departments" : "Private — department only"}
                  </button>
                ))}
              </div>
            </div>
            {visibility === "PRIVATE" && (
              <div>
                <label className="block text-xs font-medium text-gray-700 mb-1">
                  Department Access
                </label>
                {canSelectAnyDept ? (
                  <MultiSelectDept
                    options={availableDepts}
                    selected={selectedDepts}
                    onChange={setSelectedDepts}
                  />
                ) : (
                  <div className="flex items-center gap-2 px-3 py-2 bg-blue-50 border border-blue-200 rounded text-xs text-blue-700">
                    <Lock size={12} />
                    <span>Restricted to your department: <strong>{userDept || "unknown"}</strong></span>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Upload progress card OR compliance block card OR drop zone */}
          {/* Compliance block card only appears when COMPLIANCE_SCAN_KB_UPLOAD=true on server */}
          {(() => {
            const deptReady = !canSelectAnyDept || visibility !== "PRIVATE" || selectedDepts.length > 0;
            if (complianceBlock) {
              const isComplianceViolation = complianceBlock.compliance_reasons?.length > 0;
              return (
                <div className="border border-red-200 bg-red-50 rounded-lg px-4 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-start gap-2 min-w-0">
                      <ShieldOff size={15} className="text-red-500 flex-shrink-0 mt-0.5" />
                      <div className="min-w-0">
                        <div className="text-xs font-semibold text-red-700">
                          {isComplianceViolation ? "File blocked by compliance policy" : "File type not supported"}
                        </div>
                        <div className="text-xs text-red-600 truncate mt-0.5">{complianceBlock.filename}</div>
                        {isComplianceViolation && (
                          <div className="flex flex-wrap gap-1 mt-1.5">
                            {complianceBlock.compliance_reasons.map(r => (
                              <span key={r} className="bg-red-100 text-red-700 border border-red-200 text-[10px] px-1.5 py-0.5 rounded font-medium">
                                {r}
                              </span>
                            ))}
                          </div>
                        )}
                        <div className="text-[10px] text-red-400 mt-1.5">
                          This file contains sensitive data and cannot be added to the knowledge base.
                        </div>
                      </div>
                    </div>
                    <button onClick={clearUpload} className="text-red-400 hover:text-red-600 flex-shrink-0 cursor-pointer">
                      <X size={13} />
                    </button>
                  </div>
                </div>
              );
            }
            return uploadStage ? (
              <UploadProgress
                file={uploadFile}
                stage={uploadStage}
                result={uploadResult}
                onDone={clearUpload}
              />
            ) : (
              <div
                ref={dropRef}
                onClick={() => { if (deptReady) fileInputRef.current?.click(); }}
                className={`border-2 border-dashed rounded-lg p-10 text-center transition ${
                  !deptReady
                    ? "border-gray-200 bg-gray-50 opacity-70 cursor-not-allowed"
                    : isDragging
                      ? "border-gray-400 bg-gray-50 cursor-pointer"
                      : "border-gray-300 hover:border-indigo-300 hover:bg-indigo-50 cursor-pointer"
                }`}
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  className="hidden"
                  accept=".pdf,.docx,.md,.ppt,.pptx,.html,.txt"
                  onChange={e => {
                      const selected = Array.from(e.target.files);
                      e.target.value = '';   // ← reset so same file can be re-selected
                      if (selected.length > 1) { setError("Only one file can be uploaded at a time. Please select a single file."); return; }
                      if (selected.length === 1) {
                        const file = selected[0];
                        const ext = file.name.split(".").pop().toLowerCase();
                        const allowedExts = ["pdf", "docx", "md", "ppt", "pptx", "html", "txt"];
                        if (!allowedExts.includes(ext)) {
                          setError(`Unsupported file type ".${ext}". Only PDF, DOCX, MD, PPTX, HTML, and TXT files are allowed.`);
                          return;
                        }
                      }
                      handleUpload(selected);
                  }}
                />
                <Upload size={24} className="mx-auto text-gray-300 mb-2" />
                {!deptReady ? (
                  <div className="text-sm text-gray-600 font-medium">
                    Select a department above to enable upload
                  </div>
                ) : (
                  <>
                    <div className="text-sm text-gray-500 font-medium">
                      Drag & drop files, or click to browse
                    </div>
                    <div className="text-xs text-gray-400 mt-1">
                      Maximum file size: 25 MB
                    </div>
                  </>
                )}
                <div className="mt-3 flex flex-wrap justify-center gap-1">
                  {SUPPORTED_TYPES.map(t => (
                    <span key={t} className="bg-gray-100 text-gray-400 text-[10px] px-2 py-0.5 rounded">
                      {t}
                    </span>
                  ))}
                </div>
              </div>
            );
          })()}

          {/* Error banner */}
          {error && (
            <div className="flex items-start gap-3 px-4 py-3 bg-red-50 border border-red-200 rounded-lg">
                          {/* Icon */}
                          <div className="flex-shrink-0 mt-0.5">
                            <X size={16} className="text-red-500" />
                          </div>

                          {/* Message */}
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-medium text-red-700">Upload Error</p>
                            <p className="text-sm text-red-600 mt-0.5">{error}</p>
                          </div>

                          {/* ── Close BUTTON (not plain text) ──────────────────────────── */}
                          <button
                            type="button"
                            onClick={() => setError(null)}
                            className="
                              flex-shrink-0
                              px-3 py-1
                              text-xs font-medium
                              text-red-600
                              bg-white
                              border border-red-300
                              rounded
                              hover:bg-red-50
                              focus:outline-none
                              focus:ring-2
                              focus:ring-red-400
                              cursor-pointer
                              transition-colors
                            "
                          >
                            Close
              </button>
            </div>
          )}

        </div>
        </div>

      </div>
      {/* end RIGHT PANEL */}
      </>
      )}
      {/* end Chat-vs-Upload ternary */}

      </div>
      {/* end PAGE BODY */}

    </div>
  );
}
