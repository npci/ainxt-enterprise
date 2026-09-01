// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useRef } from "react";
import {
  Bell, Bot, GitBranch, MessageCircle, DollarSign, CheckCheck, Trash2,
  CheckCircle2, XCircle, ShieldCheck, AlertTriangle, ExternalLink,
  GitPullRequest, BookOpen, Hash, FileText, UserCog, X, Target,
  ChevronDown, ChevronRight, Download, BarChart2, TrendingUp,
  Activity, Zap, Award,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toIST } from "../utils/time";
import { API_BASE as API, authFetch } from '../config';
import { useConfirm } from "./ui/DialogProvider";
import WorkflowPreview, { AgentDetail } from "./WorkflowPreview";
import { setSessionData, getSessionData } from '../utils/storageUtils';
import { validateFreeText } from '../utils/securityValidation';

// User identity comes from the server — never from localStorage

const TYPE_ICONS = {
  agent_run:              Bot,
  agent_completion:       CheckCircle2,
  workflow:               GitBranch,
  workflow_completion:    CheckCircle2,
  thread_response:        MessageCircle,
  discussion_mention:     MessageCircle,
  budget_alert:           DollarSign,
  budget_request:         DollarSign,
  budget_approved:        CheckCircle2,
  budget_rejected:        XCircle,
  approval_required:      ShieldCheck,
  governance_approval:    ShieldCheck,
  sdlc_approval_required: ShieldCheck,
  kb_approval:            FileText,
  product_approval:       ShieldCheck,
  codebase_approval:      ShieldCheck,
  product_result:         CheckCircle2,
  level_override:         UserCog,
  bug_triage:             AlertTriangle,
  design_approval:        ShieldCheck,
  solution_approval:      ShieldCheck,
  pr_approval:            GitPullRequest,
  failure:                XCircle,
  agent_failure:          XCircle,
  workflow_failed:        XCircle,
  coach_digest:           Target,
};

const TYPE_COLORS = {
  agent_completion:       "text-green-600",
  workflow_completion:    "text-green-600",
  design_approval:        "text-yellow-600",
  solution_approval:      "text-yellow-600",
  pr_approval:            "text-blue-600",
  budget_alert:           "text-orange-500",
  budget_request:         "text-orange-500",
  budget_approved:        "text-green-600",
  budget_rejected:        "text-red-500",
  approval_required:      "text-yellow-600",
  governance_approval:    "text-yellow-600",
  sdlc_approval_required: "text-yellow-600",
  kb_approval:            "text-yellow-600",
  product_approval:       "text-yellow-600",
  codebase_approval:      "text-yellow-600",
  product_result:         "text-green-600",
  level_override:         "text-blue-600",
  discussion_mention:     "text-purple-600",
  failure:                "text-red-500",
  agent_failure:          "text-red-500",
  workflow_failed:        "text-red-500",
  coach_digest:           "text-indigo-600",
};

const TYPE_BG = {
  agent_completion:       "bg-green-50 border-green-200",
  workflow_completion:    "bg-green-50 border-green-200",
  design_approval:        "bg-yellow-50 border-yellow-200",
  solution_approval:      "bg-yellow-50 border-yellow-200",
  pr_approval:            "bg-blue-50 border-blue-200",
  budget_alert:           "bg-orange-50 border-orange-200",
  budget_request:         "bg-orange-50 border-orange-200",
  budget_approved:        "bg-green-50 border-green-200",
  budget_rejected:        "bg-red-50 border-red-200",
  approval_required:      "bg-yellow-50 border-yellow-200",
  governance_approval:    "bg-yellow-50 border-yellow-200",
  sdlc_approval_required: "bg-yellow-50 border-yellow-200",
  kb_approval:            "bg-yellow-50 border-yellow-200",
  product_approval:       "bg-yellow-50 border-yellow-200",
  codebase_approval:      "bg-yellow-50 border-yellow-200",
  product_result:         "bg-green-50 border-green-200",
  level_override:         "bg-blue-50 border-blue-200",
  thread_response:        "bg-purple-50 border-purple-200",
  discussion_mention:     "bg-purple-50 border-purple-200",
  failure:                "bg-red-50 border-red-200",
  agent_failure:          "bg-red-50 border-red-200",
  workflow_failed:        "bg-red-50 border-red-200",
  coach_digest:           "bg-indigo-50 border-indigo-200",
};

const TYPE_LABELS = {
  agent_run:              "Agent Run",
  agent_completion:       "Agent Done",
  workflow:               "Workflow",
  workflow_completion:    "Workflow Done",
  thread_response:        "Thread",
  discussion_mention:     "Discussion",
  budget_alert:           "Budget",
  budget_request:         "Budget Request",
  budget_approved:        "Budget Approved",
  budget_rejected:        "Budget Rejected",
  approval_required:      "Approval",
  governance_approval:    "Needs Approval",
  sdlc_approval_required: "SDLC Approval",
  kb_approval:            "KB Doc Approval",
  product_approval:       "Product Approval",
  codebase_approval:      "Codebase Approval",
  product_result:         "Product Decision",
  level_override:         "Level Override",
  bug_triage:             "Bug Triage",
  design_approval:        "Design Approval",
  solution_approval:      "Solution Approval",
  pr_approval:            "PR Review",
  failure:                "Failure",
  agent_failure:          "Agent Failure",
  workflow_failed:        "Workflow Failure",
  coach_digest:           "Coach Digest",
};

// Per-type label for the timestamp shown in list and detail panel
const TYPE_TIMESTAMP_LABELS = {
  kb_approval:            "Uploaded",
  governance_approval:    "Submitted",
  product_approval:       "Submitted",
  codebase_approval:      "Requested",
  sdlc_approval_required: "Triggered",
  design_approval:        "Submitted",
  solution_approval:      "Submitted",
  pr_approval:            "Submitted",
  agent_completion:       "Completed",
  workflow_completion:    "Completed",
  agent_run:              "Started",
  workflow:               "Started",
  budget_alert:           "Triggered",
  budget_request:         "Requested",
  budget_approved:        "Approved",
  budget_rejected:        "Rejected",
  thread_response:        "Received",
  level_override:         "Requested",
  bug_triage:             "Triggered",
  failure:                "Occurred",
  agent_failure:          "Occurred",
  workflow_failed:        "Occurred",
  coach_digest:           "Sent",
};

// ── Status-aware label overlay for governance items ────────────────────────
// A governance_approval row carries the artifact's lifecycle status in
// ``metadata.status``. The static ``TYPE_LABELS`` entry ("Needs Approval")
// only matches PENDING; once the artifact is APPROVED / REJECTED / etc. the
// chip must reflect the outcome so the approver can tell at a glance whether
// the item is still actionable.
const GOV_STATUS_CHIP = {
  APPROVED:         { label: "Approved",     cls: "bg-emerald-50 text-emerald-600 border-emerald-200" },
  PRODUCTION:       { label: "Approved",     cls: "bg-emerald-50 text-emerald-600 border-emerald-200" },
  REJECTED:         { label: "Rejected",     cls: "bg-rose-50 text-rose-600 border-rose-200" },
  DRAFT:            { label: "Cancelled",    cls: "bg-gray-50 text-gray-500 border-gray-200" },
  DEPRECATED:       { label: "Deprecated",   cls: "bg-gray-50 text-gray-500 border-gray-200" },
  PENDING_APPROVAL: { label: "Needs Approval", cls: null },  // fall back to the type's default chip
  PENDING_L2:       { label: "Needs L2",     cls: null },
};

/** Return the chip descriptor for an item — status-aware for governance items,
 *  falling back to the type-level label for everything else. */
function typeChipFor(item, defaultCls) {
  const isGov = item.type === "governance_approval";
  const status = isGov ? (item.metadata?.current_status || item.metadata?.status) : null;
  const override = status ? GOV_STATUS_CHIP[status] : null;
  const label = override?.label || (TYPE_LABELS[item.type] || item.type);
  const cls = override?.cls || defaultCls;
  return { label, cls, status };
}

const PRIORITY_COLORS = {
  High:   "bg-red-100 text-red-700",
  Medium: "bg-yellow-100 text-yellow-700",
  Low:    "bg-green-100 text-green-700",
};

// Extract priority from body text e.g. "Priority: High."
function parsePriority(body) {
  const m = (body || "").match(/Priority:\s*(High|Medium|Low)/i);
  return m ? m[1] : null;
}

// Metadata link cards
function MetaLinks({ meta }) {
  if (!meta || Object.keys(meta).length === 0) return null;

  const links = [];

  if (meta.jira_url && meta.jira_url.startsWith("http")) {
    links.push({ label: "Jira Issue", url: meta.jira_url, icon: ExternalLink, color: "bg-blue-50 border-blue-200 text-blue-700 hover:bg-blue-100" });
  }
  if (meta.gh_url) {
    // gh_url may be "Issue created: https://..." so extract URL
    const urlMatch = meta.gh_url.match(/https?:\/\/[^\s]+/);
    const url = urlMatch ? urlMatch[0] : null;
    if (url) {
      links.push({ label: "GitLab Issue", url, icon: GitBranch, color: "bg-gray-50 border-gray-200 text-gray-700 hover:bg-gray-100" });
    }
  }
  if (meta.confluence_url && meta.confluence_url.startsWith("http")) {
    links.push({ label: "Confluence Page", url: meta.confluence_url, icon: BookOpen, color: "bg-teal-50 border-teal-200 text-teal-700 hover:bg-teal-100" });
  }
  if (meta.pr_url && meta.pr_url.startsWith("http")) {
    links.push({ label: `PR #${meta.pr_number || ""}`, url: meta.pr_url, icon: GitPullRequest, color: "bg-purple-50 border-purple-200 text-purple-700 hover:bg-purple-100" });
  }

  const chips = [];
  if (meta.sdlc_run_id) {
    chips.push({ label: "SDLC Run", value: meta.sdlc_run_id.slice(0, 8) + "…" });
  }
  if (meta.jira_key) {
    chips.push({ label: "Jira Key", value: meta.jira_key });
  }
  if (meta.severity) {
    chips.push({ label: "Severity", value: meta.severity });
  }
  if (meta.score != null) {
    chips.push({ label: "Review Score", value: String(meta.score) });
  }
  if (meta.revision != null) {
    chips.push({ label: "Revision", value: `#${meta.revision}` });
  }

  if (links.length === 0 && chips.length === 0) return null;

  return (
    <div className="mt-5 space-y-3">
      {links.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {links.map(({ label, url, icon: Icon, color }) => (
            <a
              key={label}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-medium transition ${color}`}
            >
              <Icon size={12} />
              {label}
              <ExternalLink size={10} className="opacity-60" />
            </a>
          ))}
        </div>
      )}
      {chips.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {chips.map(({ label, value }) => (
            <span key={label} className="inline-flex items-center gap-1 px-2.5 py-1 bg-gray-100 text-gray-600 rounded text-xs">
              <Hash size={10} className="opacity-50" />
              <span className="font-medium">{label}:</span> {value}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Multi-select filter dropdown ─────────────────────────────
const FILTER_OPTIONS = [
  "governance_approval",
  "kb_approval",
  "product_approval",
  "codebase_approval",
  "budget_request",
  "level_override",
  "thread_response",
  "design_approval",
  "solution_approval",
  "pr_approval",
  "agent_completion",
  "product_result",
  "failure",
  "budget_alert",
  "coach_digest",
];

function FilterDropdown({ activeFilters, onChange }) {
  const [open, setOpen]   = useState(false);
  const [search, setSearch] = useState("");
  const ref = useRef(null);

  useEffect(() => {
    function handleClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const visible = FILTER_OPTIONS.filter(f =>
    !search || (TYPE_LABELS[f] || f).toLowerCase().includes(search.toLowerCase())
  );

  function toggle(f) {
    onChange(prev =>
      prev.includes(f) ? prev.filter(x => x !== f) : [...prev, f]
    );
  }

  const label = activeFilters.length === 0
    ? "All types"
    : activeFilters.length === 1
      ? (TYPE_LABELS[activeFilters[0]] || activeFilters[0])
      : `${activeFilters.length} types selected`;

  return (
    <div ref={ref} className="relative px-3 pb-2">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-2.5 py-1.5 text-xs border border-gray-200 rounded-md bg-white transition focus:border-indigo-300"
      >
        <span className={activeFilters.length > 0 ? "text-gray-800 font-medium" : "text-gray-400"}>
          {label}
        </span>
        <svg className={`w-3.5 h-3.5 text-gray-400 transition-transform ${open ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="absolute z-50 left-3 right-3 top-full mt-1 bg-white border border-gray-200 rounded-md shadow-lg">
          {/* Search inside dropdown */}
          <div className="p-2 border-b border-gray-100">
            <input
              autoFocus
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search types..."
              className="w-full px-2 py-1 text-xs border border-gray-200 rounded outline-none focus:border-indigo-300 bg-gray-50"
            />
          </div>

          {/* Clear all */}
          {activeFilters.length > 0 && (
            <button
              onClick={() => { onChange([]); setOpen(false); }}
              className="w-full cursor-pointer text-left px-3 py-1.5 text-xs text-indigo-700 hover:bg-indigo-50 border-b border-gray-100"
            >
              Clear filters
            </button>
          )}

          {/* Options */}
          <div className="max-h-52 overflow-y-auto">
            {visible.length === 0 && (
              <div className="px-3 py-2 text-xs text-gray-400">No match</div>
            )}
            {visible.map(f => (
              <label key={f} className="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-50 cursor-pointer">
                <input
                  type="checkbox"
                  checked={activeFilters.includes(f)}
                  onChange={() => toggle(f)}
                   className="accent-indigo-700"
                />
                <span className="text-xs text-gray-700">{TYPE_LABELS[f] || f}</span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// Persist read status for live- items across tab switches (cleared on full page reload)
const LIVE_READ_KEY = "inbox_live_read_ids";

// Maker-checker: true when the current user is the submitter of a governance
// item. Used to hide the "Submitted agent/workflow/skill" preview blocks (the
// maker already knows what they submitted) and to suppress Approve/Reject in
// UniversalInboxActions. ``submitted_by`` is the maker's email; ``owner_id``
// is the maker's user id — either match counts.
function isSelfSubmission(meta, me) {
  if (!meta || !me) return false;
  if (meta.submitted_by && me.email &&
      String(meta.submitted_by).toLowerCase() === String(me.email).toLowerCase()) return true;
  if (meta.owner_id && me.id &&
      String(meta.owner_id) === String(me.id)) return true;
  return false;
}

function getLiveReadIds() {
  try { return new Set(getSessionData(LIVE_READ_KEY) || []); }
  catch { return new Set(); }
}
function _sanitizeId(id) {
  const _s = String(id ?? '').replace(/[^a-zA-Z0-9_\-]/g, '');
  const _m = _s.match(/^([a-zA-Z0-9_\-]+)$/);
  return _m ? _m[1] : null;
}
function saveLiveReadId(id) {
  const _safeId = _sanitizeId(id);
  if (!_safeId) return;
  const ids = getLiveReadIds();
  ids.add(_safeId);
  setSessionData(LIVE_READ_KEY, [...ids]);
}
function saveAllLiveReadIds(ids) {
  const _safeIds = (Array.isArray(ids) ? ids : [...ids]).map(_sanitizeId).filter(Boolean);
  setSessionData(LIVE_READ_KEY, _safeIds);
}

// ─────────────────────────────────────────────────────────────────────────────
// CoachDigestCard — rich visual rendering for coach_digest inbox items
// Renders score ring, category table, recommendations, task breakdown, and
// usage breakdown — all from structured metadata, no HTML blob required.
// ─────────────────────────────────────────────────────────────────────────────

const DOMAIN_COLOURS = {
  code:     { bar: "#4f46e5", bg: "bg-indigo-50",  border: "border-indigo-200",  text: "text-indigo-700"  },
  devops:   { bar: "#7c3aed", bg: "bg-violet-50",  border: "border-violet-200",  text: "text-violet-700"  },
  data:     { bar: "#0891b2", bg: "bg-cyan-50",    border: "border-cyan-200",    text: "text-cyan-700"    },
  security: { bar: "#dc2626", bg: "bg-red-50",     border: "border-red-200",     text: "text-red-700"     },
  finance:  { bar: "#059669", bg: "bg-emerald-50", border: "border-emerald-200", text: "text-emerald-700" },
  hr:       { bar: "#db2777", bg: "bg-pink-50",    border: "border-pink-200",    text: "text-pink-700"    },
  legal:    { bar: "#d97706", bg: "bg-amber-50",   border: "border-amber-200",   text: "text-amber-700"   },
  general:  { bar: "#64748b", bg: "bg-slate-50",   border: "border-slate-200",   text: "text-slate-600"   },
};

const SEVERITY_COLOUR = {
  critical: "bg-red-100 text-red-700 border-red-200",
  high:     "bg-orange-100 text-orange-700 border-orange-200",
  medium:   "bg-amber-100 text-amber-700 border-amber-200",
  low:      "bg-slate-100 text-slate-600 border-slate-200",
};

const CAT_COLOUR = {
  "prompt-quality":     "text-indigo-600",
  "session-hygiene":    "text-violet-600",
  "review-discipline":  "text-amber-600",
  "tool-mastery":       "text-cyan-600",
  "context-management": "text-emerald-600",
  "security":           "text-red-600",
};

function ScoreRing({ score }) {
  const valid = typeof score === "number";
  const pct   = valid ? Math.min(100, Math.max(0, score)) : 0;
  const r = 36, cx = 44, cy = 44, stroke = 7;
  const circ = 2 * Math.PI * r;
  const dash  = (pct / 100) * circ;
  const colour = pct >= 75 ? "#10b981" : pct >= 50 ? "#f59e0b" : "#ef4444";

  return (
    <div className="flex flex-col items-center gap-1">
      <svg width={88} height={88} className="drop-shadow-sm">
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="#e2e8f0" strokeWidth={stroke} />
        <circle
          cx={cx} cy={cy} r={r} fill="none"
          stroke={colour} strokeWidth={stroke}
          strokeDasharray={`${dash} ${circ}`}
          strokeLinecap="round"
          transform={`rotate(-90 ${cx} ${cy})`}
          style={{ transition: "stroke-dasharray 0.6s ease" }}
        />
        <text x={cx} y={cy + 1} textAnchor="middle" dominantBaseline="middle"
              fontSize="15" fontWeight="700" fill={colour}>
          {valid ? Math.round(pct) : "—"}
        </text>
        {valid && (
          <text x={cx} y={cy + 14} textAnchor="middle" dominantBaseline="middle"
                fontSize="8" fill="#94a3b8">
            /100
          </text>
        )}
      </svg>
      <span className="text-[10px] font-semibold text-slate-500 uppercase tracking-widest">Practice score</span>
    </div>
  );
}

function CategoryBar({ label, value }) {
  const pct = typeof value === "number" ? Math.min(100, Math.max(0, value)) : 0;
  const colour = pct >= 75 ? "#10b981" : pct >= 50 ? "#f59e0b" : "#ef4444";
  const catColour = CAT_COLOUR[label] || "text-slate-600";
  return (
    <div className="flex items-center gap-2 py-1">
      <span className={`text-[11px] font-medium w-36 flex-shrink-0 truncate capitalize ${catColour}`}>
        {label.replace(/-/g, " ")}
      </span>
      <div className="flex-1 h-2 bg-slate-100 rounded-full overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${pct}%`, background: colour }}
        />
      </div>
      <span className="text-[11px] font-semibold text-slate-600 w-8 text-right flex-shrink-0">
        {typeof value === "number" ? Math.round(value) : "—"}
      </span>
    </div>
  );
}

function CoachDigestCard({ item }) {
  const meta = item.metadata || {};
  const [taOpen, setTaOpen] = useState(null);

  // Structured data from metadata
  const overall     = meta.overall_score ?? meta.overall ?? null;
  const scores      = meta.scores || {};
  const categories  = scores.categories || {};
  const eventCount  = scores.event_count ?? null;
  const recs        = meta.recs || [];
  const usage       = meta.usage || {};
  const taskAnalysis = meta.task_analysis || null;
  const fromAdmin   = meta.from || null;
  const kind        = meta.kind || "coaching_note";

  const hasCategories = Object.keys(categories).length > 0;
  const hasRecs       = recs.length > 0;
  const hasUsage      = (usage.channels || []).length > 0;
  const hasTA         = taskAnalysis && (taskAnalysis.domains || []).length > 0;

  // If we have an html_body but no structured data, fall back to iframe
  if (!overall && !hasRecs && !hasTA && !hasUsage && meta.html_body) {
    return (
      <iframe
        title="AiNxt Coach digest"
        srcDoc={meta.html_body}
        sandbox=""
        className="w-full min-h-[720px] rounded-2xl border border-indigo-100 bg-white shadow-sm"
      />
    );
  }

  return (
    <div className="space-y-4">

      {/* ── Hero banner ─────────────────────────────────────────────── */}
      <div className="rounded-2xl p-5 text-white"
           style={{ background: "linear-gradient(135deg,#4f46e5,#7c3aed)" }}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="text-base font-bold leading-tight">
              {kind === "weekly_digest" ? "Your weekly AiNxt Coach summary" : "AiNxt Coach summary"}
            </h3>
            <p className="text-indigo-200 text-[11px] mt-1">
              No prompt content is stored or shown — only practice signals and scores.
            </p>
            {fromAdmin && (
              <p className="text-indigo-200 text-[11px] mt-1">Sent by: {fromAdmin}</p>
            )}
          </div>
          <ScoreRing score={overall} />
        </div>
      </div>

      {/* ── Stats row ───────────────────────────────────────────────── */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: "Events analysed", value: eventCount ?? (scores.event_count ?? "—"), icon: Activity, accent: "indigo" },
          { label: "Spend observed",  value: usage.cost_usd != null ? `$${Number(usage.cost_usd).toFixed(4)}` : "—", icon: TrendingUp, accent: "emerald" },
          { label: "Task types",      value: hasTA ? taskAnalysis.domains.length : "—", icon: BarChart2, accent: "violet" },
        ].map(({ label, value, icon: Icon, accent }) => {
          const accentMap = {
            indigo:  { bg: "bg-indigo-50",  icon: "text-indigo-500",  val: "text-indigo-700"  },
            emerald: { bg: "bg-emerald-50", icon: "text-emerald-500", val: "text-emerald-700" },
            violet:  { bg: "bg-violet-50",  icon: "text-violet-500",  val: "text-violet-700"  },
          };
          const a = accentMap[accent];
          return (
            <div key={label} className={`rounded-xl p-3 border border-slate-200 ${a.bg}`}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] uppercase tracking-widest text-slate-500 font-bold">{label}</span>
                <Icon size={13} className={a.icon} />
              </div>
              <div className={`text-xl font-bold ${a.val}`}>{value}</div>
            </div>
          );
        })}
      </div>

      {/* ── Category scores ─────────────────────────────────────────── */}
      {hasCategories && (
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <Award size={14} className="text-indigo-500" />
            <span className="text-[11px] font-bold uppercase tracking-widest text-slate-600">Category scores</span>
          </div>
          <div className="space-y-0.5">
            {Object.entries(categories).map(([cat, val]) => (
              <CategoryBar key={cat} label={cat} value={val} />
            ))}
          </div>
        </div>
      )}

      {/* ── Top recommendations ─────────────────────────────────────── */}
      {hasRecs && (
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <Zap size={14} className="text-amber-500" />
            <span className="text-[11px] font-bold uppercase tracking-widest text-slate-600">Top opportunities</span>
          </div>
          <div className="space-y-2">
            {recs.map((r, i) => (
              <div key={i} className="flex items-start gap-3 py-2 border-b border-slate-50 last:border-0">
                <span className="w-5 h-5 rounded-full bg-indigo-100 text-indigo-700 text-[10px] font-bold
                                 flex items-center justify-center flex-shrink-0 mt-0.5">
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[12px] font-semibold text-slate-800">{r.title}</span>
                    {r.severity && (
                      <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded-full border ${SEVERITY_COLOUR[r.severity] || SEVERITY_COLOUR.low}`}>
                        {r.severity}
                      </span>
                    )}
                    {r.count > 0 && (
                      <span className="text-[10px] text-slate-400">×{r.count}</span>
                    )}
                  </div>
                  <p className="text-[11.5px] text-slate-600 mt-0.5 leading-relaxed">{r.advice}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Task-type breakdown ─────────────────────────────────────── */}
      {hasTA && (
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
          <div className="flex items-center gap-2 mb-1">
            <BarChart2 size={14} className="text-violet-500" />
            <span className="text-[11px] font-bold uppercase tracking-widest text-slate-600">Task-type breakdown</span>
          </div>
          {taskAnalysis.summary && (
            <p className="text-[11.5px] text-slate-500 mb-3 leading-relaxed">{taskAnalysis.summary}</p>
          )}

          {/* Domain bars + expandable tips */}
          <div className="space-y-2">
            {taskAnalysis.domains.map(d => {
              const c = DOMAIN_COLOURS[d.domain] || DOMAIN_COLOURS.general;
              const isOpen = taOpen === d.domain;
              return (
                <div key={d.domain} className={`border rounded-xl overflow-hidden ${c.border} ${c.bg}`}>
                  <button
                    onClick={() => setTaOpen(isOpen ? null : d.domain)}
                    className="w-full flex items-center gap-3 px-3 py-2.5 text-left"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between mb-1">
                        <span className={`text-[11.5px] font-semibold ${c.text}`}>{d.label}</span>
                        <span className="text-[10px] text-slate-500 ml-2 flex-shrink-0">
                          {d.count} interaction{d.count !== 1 ? "s" : ""} · {d.pct}%
                        </span>
                      </div>
                      <div className="h-2 bg-white/60 rounded-full overflow-hidden border border-white/40">
                        <div
                          className="h-full rounded-full"
                          style={{ width: `${d.pct}%`, background: c.bar, transition: "width 0.5s ease" }}
                        />
                      </div>
                    </div>
                    <ChevronRight
                      size={14}
                      className={`flex-shrink-0 ${c.text} transition-transform ${isOpen ? "rotate-90" : ""}`}
                    />
                  </button>

                  {isOpen && (
                    <div className="px-3 pb-3 border-t border-white/50 space-y-2">
                      {(d.top_issues || []).length === 0 ? (
                        <p className="text-[11px] text-emerald-600 pt-2">
                          ✓ No recurring issues for this task type — great work!
                        </p>
                      ) : (
                        (d.top_issues || []).map((issue, idx) => (
                          <div key={idx} className="pt-2">
                            <div className="flex items-center gap-1.5 mb-0.5">
                              <span className={`text-[9.5px] font-bold uppercase tracking-wider px-1.5 py-0.5
                                              rounded-full bg-white/70 ${c.text}`}>
                                {issue.category}
                              </span>
                              <span className="text-[10px] text-slate-400">
                                ×{issue.count} hit{issue.count !== 1 ? "s" : ""}
                              </span>
                            </div>
                            <p className="text-[11px] text-slate-700 leading-relaxed">{issue.tip}</p>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ── Usage by channel ────────────────────────────────────────── */}
      {hasUsage && (
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <Activity size={14} className="text-slate-500" />
            <span className="text-[11px] font-bold uppercase tracking-widest text-slate-600">Usage by channel</span>
          </div>
          <table className="w-full text-[11.5px]">
            <thead>
              <tr className="text-[10px] uppercase tracking-widest text-slate-400 border-b border-slate-100">
                <th className="text-left pb-2 font-semibold">Channel</th>
                <th className="text-right pb-2 font-semibold">Events</th>
                <th className="text-right pb-2 font-semibold">Cost</th>
              </tr>
            </thead>
            <tbody>
              {(usage.channels || []).map((ch, i) => (
                <tr key={i} className="border-b border-slate-50 last:border-0">
                  <td className="py-1.5 font-medium text-slate-700 capitalize">{ch.channel || "unknown"}</td>
                  <td className="py-1.5 text-right text-slate-600">{ch.events}</td>
                  <td className="py-1.5 text-right text-slate-500">${Number(ch.cost_usd || 0).toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ── HTML email fallback toggle ───────────────────────────────── */}
      {meta.html_body && (
        <HtmlEmailToggle html={meta.html_body} />
      )}
    </div>
  );
}

function HtmlEmailToggle({ html }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        onClick={() => setOpen(v => !v)}
        className="flex items-center gap-1.5 text-[11px] text-indigo-500 hover:text-indigo-700 transition font-medium"
      >
        <ChevronRight size={13} className={`transition-transform ${open ? "rotate-90" : ""}`} />
        {open ? "Hide" : "View"} full HTML email
      </button>
      {open && (
        <iframe
          title="AiNxt Coach digest HTML"
          srcDoc={html}
          sandbox=""
          className="w-full mt-2 min-h-[600px] rounded-2xl border border-indigo-100 bg-white shadow-sm"
        />
      )}
    </div>
  );
}

export default function Inbox({ user, onUnreadChange }) {
  const [items, setItems] = useState([]);
  const [activeFilters, setActiveFilters] = useState([]);
  const [selected, setSelected] = useState(null);
  const [unread, setUnread] = useState(0);
  const { confirm } = useConfirm();

  // Sync unread count up to App.jsx whenever it changes
  useEffect(() => { onUnreadChange?.(unread); }, [unread]);
  const [searchQ, setSearchQ] = useState("");
  // me: normalized from user prop (preferred) or fetched from /auth/me as fallback
  const [me, setMe] = useState(null);

  useEffect(() => {
    if (user?.userId) {
      setMe({ id: user.userId, email: user.email || null, can_approve: user.can_approve ?? false });
    } else {
      authFetch(`${API}/auth/me`)
        .then(r => r.ok ? r.json() : null)
        .then(data => { if (data) setMe(data); });
    }
  }, [user?.userId]);

  useEffect(() => { if (me) loadInbox(); }, [me]);

  async function loadInbox() {
    const params = new URLSearchParams({ user: me.id, limit: 50 });

    // Fetch notification-driven items + live DB pending approvals in parallel
    const [inboxRes, liveRes] = await Promise.all([
      authFetch(`${API}/inbox?${params}`),
      authFetch(`${API}/inbox/pending-approvals?user=${me.id}`),
    ]);
    const inboxData = await inboxRes.json();
    const liveData  = liveRes.ok ? await liveRes.json() : { items: [] };

    const rawNotifItems = inboxData.items || [];
    const liveItems  = liveData.items  || [];

    // Collapse duplicate governance notifications: a single artifact can end up
    // with multiple persisted governance_approval rows for the same user (e.g.
    // a repeated submit, or overlapping approver + HOD routing). Keep only the
    // newest per (type, source_id) for governance-approval items so the list
    // shows one entry per pending artifact. Non-governance items are untouched.
    const GOV_TYPES = new Set(["governance_approval", "governance_approval_needed"]);
    const seenGov = new Set();
    const notifItems = [...rawNotifItems]
      .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
      .filter(i => {
        if (!GOV_TYPES.has(i.type) || !i.source_id) return true;
        const key = `${i.type}:${i.source_id}`;
        if (seenGov.has(key)) return false;
        seenGov.add(key);
        return true;
      });

    // Deduplicate live vs. persisted rows.
    //
    // Governance items reflect an artifact lifecycle: DRAFT → PENDING_APPROVAL
    // → APPROVED / REJECTED → …  The ``pending-approvals`` live query is the
    // authority on the *current* status of the artifact — if a live row
    // exists, the artifact is PENDING_APPROVAL right now, regardless of what
    // persisted audit rows say. This matters on resubmit-after-reject: the
    // approver still holds a persisted ``[REJECTED]`` row (their own audit
    // trail) and, depending on scope, may not have received a fresh
    // ``[Needs Approval]`` persisted mirror. Without this rule the stale
    // REJECTED row wins the collapse and hides the live pending item —
    // approver sees a ``[REJECTED]`` heading with accept/reject buttons.
    //
    // So: for governance types, when a live PENDING row and a persisted
    // terminal row share ``source_id``, drop the persisted row and keep the
    // live one. Non-governance items keep the original behaviour (persisted
    // wins to avoid double-display of notification-backed items).
    const GOV_TERMINAL = new Set(["REJECTED", "APPROVED", "DEPRECATED", "PRODUCTION", "DRAFT"]);
    const liveGovSourceIds = new Set(
      liveItems
        .filter(i => GOV_TYPES.has(i.type) && i.source_id)
        .map(i => i.source_id)
    );
    const notifItemsFiltered = notifItems.filter(i => {
      if (!GOV_TYPES.has(i.type) || !i.source_id) return true;
      if (!liveGovSourceIds.has(i.source_id)) return true;
      const st = (i.metadata?.current_status || i.metadata?.status || "").toUpperCase();
      // Persisted row is a terminal/audit state but the artifact is live-
      // PENDING again — the live row is the source of truth, drop this one.
      return !GOV_TERMINAL.has(st);
    });
    const notifSourceIds = new Set(notifItemsFiltered.map(i => i.source_id).filter(Boolean));
    const dedupedLive = liveItems.filter(i => !notifSourceIds.has(i.source_id));

    // Restore read state for live items from sessionStorage
    const locallyRead = getLiveReadIds();
    const dedupedLiveWithRead = dedupedLive.map(i =>
      locallyRead.has(String(i.id)) ? { ...i, read: true } : i
    );

    // Merge: notification items first (they have read/unread state), then live items
    const merged = [...notifItemsFiltered, ...dedupedLiveWithRead];
    merged.sort((a, b) => b.created_at - a.created_at);

    setItems(merged);
    // Only count live items that haven't been locally marked as read
    const unreadLive = dedupedLiveWithRead.filter(i => !i.read).length;
    setUnread((inboxData.unread_count || 0) + unreadLive);
  }

  async function markRead(itemId) {
    if (String(itemId).startsWith("live-")) {
      // Live items have no DB row — persist read state in sessionStorage
      saveLiveReadId(String(itemId));
      setItems(prev => prev.map(i => i.id === itemId ? { ...i, read: true } : i));
      setUnread(prev => Math.max(0, prev - 1));
      return;
    }
    await authFetch(`${API}/inbox/${itemId}/read`, { method: "POST" });
    setItems(prev => prev.map(i => i.id === itemId ? { ...i, read: true } : i));
    setUnread(prev => Math.max(0, prev - 1));
  }

  async function markAllRead() {
    await authFetch(`${API}/inbox/read-all?user=${me.id}`, { method: "POST" });
    // Persist all current live item IDs as read in sessionStorage
    const liveIds = items.filter(i => String(i.id).startsWith("live-")).map(i => String(i.id));
    saveAllLiveReadIds(liveIds);
    setItems(prev => prev.map(i => ({ ...i, read: true })));
    setUnread(0);
  }

  async function deleteItemOLD(itemId) {
    if (String(itemId).startsWith("live-")) return; // live DB items can't be dismissed
    await authFetch(`${API}/inbox/${itemId}?user=${me.id}`, { method: "DELETE" });
    setItems(prev => prev.filter(i => i.id !== itemId));
    if (selected?.id === itemId) setSelected(null);
  }

 async function deleteItem(item) {
    const ok = await confirm({ title: "Delete Product", message: `Delete "${item?.title}"? This cannot be undone.`, confirmLabel: "Delete" });
    if (!ok) return;
    try {
      if (String(item?.id).startsWith("live-")) return; 
      const res = await authFetch(`${API}/inbox/${item?.id}?user=${me.id}`, { method: "DELETE" });
      if (!res.ok && res.status !== 204) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Delete failed");
      }
     setItems(prev => prev.filter(i => i.id !== item?.id));
    if (selected?.id === item?.id) setSelected(null);
    } catch (err) {
     }
  }


  function selectItem(item) {
    setSelected(item);
    if (!item.read) markRead(item.id);
  }

  const filtered = items.filter(item => {
    if (activeFilters.length > 0 && !activeFilters.includes(item.type)) return false;
    if (searchQ) {
      const q = searchQ.toLowerCase();
      return (item.title || "").toLowerCase().includes(q) ||
             (item.body  || "").toLowerCase().includes(q);
    }
    return true;
  });

  return (
    <div className="flex h-full">

      {/* ── LEFT: List ── */}
      <div className="w-72 bg-gray-50 border-r border-gray-200 flex flex-col">

        {/* Header */}
        <div className="px-4 py-3.5 border-b border-gray-200 flex items-center justify-between bg-white">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 bg-indigo-100 rounded-lg flex items-center justify-center">
              <Bell size={15} className="text-indigo-600" />
            </div>
            <div>
              <span className="text-sm font-semibold  text-indigo-700">Inbox</span>
              {unread > 0 && <span className="ml-2 bg-amber-500 text-white text-[10px] rounded-full px-1.5 py-0.5 font-bold">{unread}</span>}
            </div>
          </div>
          {unread > 0 && (
            <button onClick={markAllRead} className="flex items-center gap-1 cursor-pointer hover:bg-indigo-50 p-1 rounded text-xs text-indigo-600 hover:text-indigo-800 font-medium">
              <CheckCheck size={12} /> All read
            </button>
          )}
        </div>

        {/* Search */}
        <div className="px-3 pt-2 pb-1">
          <input
            value={searchQ}
            onChange={e => setSearchQ(e.target.value)}
            placeholder="Search inbox..."
            className="w-full px-2.5 py-1.5 text-xs border border-gray-200 rounded-md outline-none focus:border-indigo-300 bg-white shadow-sm"
          />
        </div>

        {/* Filter dropdown */}
        <div className="border-b border-gray-100 pb-2">
          <FilterDropdown activeFilters={activeFilters} onChange={setActiveFilters} />
        </div>

        {/* List */}
        <div className="flex-1 overflow-y-auto">
          {filtered.length === 0 && (
            <div className="p-6 text-center text-gray-400 text-sm">No notifications</div>
          )}
          {filtered.map(item => {
            const Icon = TYPE_ICONS[item.type] || Bell;
            const iconColor = TYPE_COLORS[item.type] || "text-gray-500";
            const bgColor = TYPE_BG[item.type] || "bg-gray-50 border-gray-200";
            // For kb_approval: prefer the actual document upload time stored in metadata.uploaded_at
            // over the inbox notification creation time (item.created_at), which is slightly later.
            const _rawTs = (item.type === "kb_approval" && item.metadata?.uploaded_at)
              ? new Date(item.metadata.uploaded_at)
              : (item.created_at > 1e10 ? new Date(item.created_at) : new Date(item.created_at * 1000));
            const ts = _rawTs;
            const tsLabel = TYPE_TIMESTAMP_LABELS[item.type] || "Received";
            const priority = parsePriority(item.body) || item.metadata?.priority;
            const bodyPreview = (item.body || "").replace(/^Priority:\s*(High|Medium|Low)\.\s*/i, "").split("\n")[0];
            const isApproval = ["governance_approval","kb_approval","product_approval","codebase_approval","sdlc_approval_required","design_approval","solution_approval","pr_approval"].includes(item.type);

            return (
              <div
                key={item.id}
                onClick={() => selectItem(item)}
                className={`relative flex gap-3 px-4 py-3.5 m-1 rounded cursor-pointer border-b border-gray-100 transition-colors
                  ${selected?.id === item.id ? "bg-indigo-50 border-l-2 border-l-indigo-500" : "hover:bg-gray-100 hover:text-gray-800"}
                  ${!item.read ? "" : "opacity-85"}`}
              >
                {/* Unread dot */}
                {!item.read && (
                  <div className="absolute left-1.5 top-1/2 -translate-y-1/2 w-1.5 h-1.5 bg-indigo-500 rounded-full" />
                )}

                {/* Icon avatar */}
                <div className={`w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0 border ${bgColor}`}>
                  <Icon size={16} className={iconColor} />
                </div>

                <div className="flex-1 min-w-0">
                  <div className="flex items-start justify-between gap-1">
                    <span className={`text-sm leading-snug break-all ${!item.read ? "font-semibold text-gray-800" : "font-medium text-gray-600"} ${selected?.id === item.id ? 'text-indigo-700':''}`}>
                      {item.title}
                    </span>
                    <button
                      onClick={e => { e.stopPropagation(); deleteItem(item); }}
                      className="p-0.5 hover:bg-red-200 rounded flex-shrink-0 text-gray-300 hover:text-red-500 transition-colors cursor-pointer"
                    >
                      <X size={12} />
                    </button>
                  </div>

                  <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                    {(() => {
                      // Status-aware chip: for governance items the label
                      // reflects APPROVED / REJECTED / etc. so a resolved
                      // item never gets a stale "Needs Approval" chip.
                      const chip = typeChipFor(item, `border ${bgColor} ${iconColor}`);
                      const cls = chip.cls.startsWith("border ") ? chip.cls : `border ${chip.cls}`;
                      return (
                        <span className={`text-[10px] px-2 py-0.5 rounded-full font-semibold ${cls}`}>
                          {chip.label}
                        </span>
                      );
                    })()}
                    {priority && (
                      <span className={`text-[10px] px-2 py-0.5 rounded-full font-semibold ${PRIORITY_COLORS[priority] || "bg-gray-100 text-gray-600"}`}>
                        {priority}
                      </span>
                    )}
                    {/* "Needs Action" is only meaningful while the item is
                        still actionable. Governance items resolved into
                        APPROVED/REJECTED/etc. must not keep the chip. */}
                    {isApproval && (item.type !== "governance_approval" ||
                      ["PENDING_APPROVAL", "PENDING_L2"].includes(
                        item.metadata?.current_status || item.metadata?.status
                      )) && (
                      <span className="text-[10px] px-2 py-0.5 rounded-full bg-amber-50 text-amber-600 font-semibold border border-amber-200">
                        Needs Action
                      </span>
                    )}
                    {/* "Raised by" is only meaningful for governance items — the
                        submitter's identity is what an approver needs at a glance. */}
                    {item.type === "governance_approval" && item.metadata?.submitted_by && (
                      <span className="text-[10px] px-2 py-0.5 rounded-full bg-gray-50 text-gray-600 font-semibold border border-gray-200">
                        Raised by: {item.metadata.submitted_by}
                      </span>
                    )}
                  </div>

                  {bodyPreview && (
                    <p className="text-xs text-gray-500 mt-1 line-clamp-1">{bodyPreview}</p>
                  )}
                  <p className="text-[10px] text-gray-400 mt-0.5">{tsLabel}: {toIST(ts)}</p>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── RIGHT: Detail ── */}
      <div className="flex-1 overflow-y-auto p-6 bg-white">
        {!selected ? (
          <div className="flex flex-col items-center justify-center h-full text-gray-300">
            <Bell size={48} />
            <p className="mt-3 text-sm">Select a notification</p>
          </div>
        ) : (
          <div className="max-w-2xl mx-auto">

            {/* Type banner — status-aware for governance items so a resolved
                artifact reads "Approved" / "Rejected" instead of the stale
                "Needs Approval" title. */}
            <div className={`flex items-center gap-3 px-4 py-3 rounded-lg border mb-5 ${TYPE_BG[selected.type] || "bg-gray-50 border-gray-200"}`}>
              {(() => { const Icon = TYPE_ICONS[selected.type] || Bell; return <Icon size={18} className={TYPE_COLORS[selected.type] || "text-gray-500"} />; })()}
              <span className={`text-sm font-semibold ${TYPE_COLORS[selected.type] || "text-gray-700"}`}>
                {typeChipFor(selected, "").label}
              </span>
              {(() => {
                const priority = parsePriority(selected.body) || selected.metadata?.priority;
                return priority ? (
                  <span className={`ml-auto text-xs px-2 py-0.5 rounded font-medium ${PRIORITY_COLORS[priority] || "bg-gray-100 text-gray-600"}`}>
                    {priority}
                  </span>
                ) : null;
              })()}
            </div>

            {/* Title + timestamp */}
            <h2 className="text-base font-semibold text-gray-800 mb-1">
              {selected.type === "kb_approval" && selected.metadata?.display_name
                ? selected.title.replace(/:\s*.+$/, `: ${selected.metadata.display_name}`)
                : selected.title}
            </h2>
            <p className="text-xs text-gray-400 mb-1">
              {TYPE_TIMESTAMP_LABELS[selected.type] || "Received"}: {toIST(
                (selected.type === "kb_approval" && selected.metadata?.uploaded_at)
                  ? new Date(selected.metadata.uploaded_at)
                  : (selected.created_at > 1e10 ? new Date(selected.created_at) : new Date(selected.created_at * 1000))
              )}
            </p>
            {/* Governance items: explicit "Raised by ... on <ts>" line plus a
                scope indicator so the approver never has to hunt for who
                submitted the request or what cohort it targets. */}
            {selected.type === "governance_approval" && selected.metadata?.submitted_by && (
              <p className="text-xs text-gray-500 mb-1">
                Raised by: <span className="font-medium text-gray-700">{selected.metadata.submitted_by}</span>
                {" "}on{" "}
                {toIST(selected.created_at > 1e10 ? new Date(selected.created_at) : new Date(selected.created_at * 1000))}
              </p>
            )}
            {selected.type === "governance_approval" && selected.metadata?.visibility && (
              <p className="text-xs text-gray-500 mb-5">
                Scope:{" "}
                <span className="font-medium text-gray-700">
                  {String(selected.metadata.visibility).toLowerCase() === "public"
                    ? "Public"
                    : (selected.metadata.department ? `Department · ${selected.metadata.department}` : "Department")}
                </span>
                {selected.metadata.sent_to && (
                  <>
                    {" "}· Sent to: <span className="font-medium text-gray-700">{selected.metadata.sent_to}</span>
                  </>
                )}
              </p>
            )}
            {selected.type !== "governance_approval" && <div className="mb-5" />}

            {selected.type === "coach_digest" ? (
              <CoachDigestCard item={selected} />
            ) : ((selected.body || selected.type === "kb_approval") && (
              <div className="prose prose-sm max-w-none text-gray-700 leading-relaxed">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {selected.type === "kb_approval" && selected.metadata?.display_name
                    ? `${selected.metadata.display_name} uploaded to namespace \`${selected.metadata.namespace}\` by \`${selected.metadata.uploaded_by || "unknown"}\`. Approve to make it searchable.`
                    : selected.body}
                </ReactMarkdown>
              </div>
            ))}

            {/* Metadata links + chips */}
            <MetaLinks meta={selected.metadata} />

            {/* Submitted workflow preview (summary + expandable graph).
                Hidden for the maker's own submission — they already know what
                they submitted, so the preview is noise. */}
            {selected.type === "governance_approval" &&
              selected.metadata?.entity_type === "workflows" &&
              !isSelfSubmission(selected.metadata, me) && (
                <WorkflowApprovalPreview key={`wfp-${selected.id}`} meta={selected.metadata} />
              )}

            {/* Submitted standalone agent preview (system prompt + tools + skills).
                Hidden for the maker's own submission. */}
            {selected.type === "governance_approval" &&
              selected.metadata?.entity_type === "agents" &&
              !isSelfSubmission(selected.metadata, me) && (
                <AgentApprovalPreview key={`agp-${selected.id}`} meta={selected.metadata} />
              )}

            {/* Submitted skill preview (code + schemas + permissions).
                Hidden for the maker's own submission. */}
            {selected.type === "governance_approval" &&
              selected.metadata?.entity_type === "skills" &&
              !isSelfSubmission(selected.metadata, me) && (
                <SkillApprovalPreview key={`skp-${selected.id}`} meta={selected.metadata} />
              )}

            {/* Action buttons for approval types */}
            <UniversalInboxActions key={selected.id} item={selected} me={me} onDone={() => loadInbox()} />

          </div>
        )}
      </div>
    </div>
  );
}

// ── Reject reason form ────────────────────────────────────────
function RejectForm({ reason, setReason, onConfirm, onCancel, busy }) {
  return (
    <div className="space-y-2 mt-2">
      <textarea
        value={reason}
        onChange={e => setReason(e.target.value)}
        placeholder="Rejection reason (required)..."
        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm resize-none h-20 outline-none focus:border-gray-400"
      />
      <div className="flex gap-2">
        <button
          onClick={onConfirm}
          disabled={!reason.trim() || busy}
          className="px-3 py-1.5 cursor-pointer brand-grad hover:opacity-70 text-white text-sm rounded font-medium disabled:opacity-40 hover:bg-red-700"
        >
          Confirm Reject
        </button>
        <button
          onClick={onCancel}
          className="px-3 py-1.5 text-sm hover:bg-gray-100 text-gray-600  border border-gray-100 cursor-pointer rounded"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

// ── One node row in the summary list (agent rows expand to detail) ──
function WorkflowNodeRow({ node }) {
  const [open, setOpen] = useState(false);
  const label = node.data?.name || node.data?.label || node.type;
  const tools = Array.isArray(node.data?.tools) ? node.data.tools : [];
  const isAgent = node.type === "agent";

  return (
    <li className="text-xs">
      <div
        className={`flex items-center justify-between px-4 py-2 ${isAgent ? "cursor-pointer hover:bg-gray-50" : ""}`}
        onClick={isAgent ? () => setOpen((v) => !v) : undefined}
      >
        <span className="flex items-center gap-2 min-w-0">
          {isAgent
            ? (open ? <ChevronDown size={13} className="text-gray-400 flex-shrink-0" /> : <ChevronRight size={13} className="text-gray-400 flex-shrink-0" />)
            : <span className="w-[13px] flex-shrink-0" />}
          <span className="uppercase tracking-wide text-[10px] font-semibold text-gray-400 w-20 flex-shrink-0">
            {node.type}
          </span>
          <span className="text-gray-700 truncate">{label}</span>
        </span>
        {isAgent && tools.length > 0 && (
          <span className="text-gray-400 flex-shrink-0">{tools.length} tool{tools.length !== 1 ? "s" : ""}</span>
        )}
      </div>
      {isAgent && open && (
        <div className="px-4 pb-3 pt-1 bg-gray-50/60 border-t border-gray-50">
          <AgentDetail data={node.data} />
        </div>
      )}
    </li>
  );
}

// ── Submitted skill preview (code + schemas + permissions) ──
// Shown for governance_approval items where entity_type === "skills". A skill is
// code-based, so the approver's key artifact is the source code plus its I/O
// schemas and permissions. Reads the approver-only
// /governance/skills/{name}/source endpoint, scoped by owner_id. Fails silently
// (403/404) so it never blocks the action panel for non-approvers.
function SkillApprovalPreview({ meta }) {
  const [state, setState] = useState("loading"); // loading | ready | error
  const [skill, setSkill] = useState(null);
  const [showCode, setShowCode] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    setSkill(null);
    setShowCode(true);
    async function load() {
      try {
        const qs = meta.owner_id ? `?owner_id=${encodeURIComponent(meta.owner_id)}` : "";
        const r = await authFetch(`${API}/governance/skills/${encodeURIComponent(meta.entity_name)}/source${qs}`);
        if (cancelled) return;
        if (!r.ok) { setState("error"); return; }
        const d = await r.json();
        setSkill(d);
        setState("ready");
      } catch (_) {
        if (!cancelled) setState("error");
      }
    }
    if (meta?.entity_name) load(); else setState("error");
    return () => { cancelled = true; };
  }, [meta?.entity_name, meta?.owner_id]);

  if (state === "loading") {
    return <div className="mt-5 text-xs text-gray-400">Loading skill preview…</div>;
  }
  if (state === "error" || !skill) {
    return null; // non-approver 403 / wrong owner 404 — stay quiet
  }

  return (
    <div className="mt-5 rounded-lg border border-gray-200 overflow-hidden">
      <div className="px-4 py-3 bg-gray-50 border-b border-gray-100">
        <div className="flex items-center gap-2 text-sm font-semibold text-gray-700">
          <BookOpen size={14} className="text-gray-500" />
          Submitted skill
        </div>
        {skill.description && (
          <p className="text-xs text-gray-500 mt-1 leading-relaxed">{skill.description}</p>
        )}
        {skill.category && (
          <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-xs text-gray-500">
            <span>Category: <span className="text-gray-700">{skill.category}</span></span>
          </div>
        )}
      </div>

      <div className="px-4 py-3 text-xs">
        {/* Source */}
        <div>
          <button
            onClick={() => setShowCode((v) => !v)}
            className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-500 hover:text-gray-700 cursor-pointer"
          >
            {showCode ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            Skill source
          </button>
          {showCode && (
            skill.code ? (
              <pre
                className="mt-1 whitespace-pre rounded-md p-3 max-h-72 overflow-auto leading-relaxed font-mono text-[11.5px]"
                style={{
                  // Explicit inline colors defeat any lingering Tailwind
                  // ordering conflict (previously `text-gray-700` came before
                  // `text-gray-100` on a near-black background and won the
                  // cascade, producing near-invisible dark-grey-on-black).
                  backgroundColor: "#0f172a",   // slate-900
                  color: "#f8fafc",             // slate-50 — high contrast
                  border: "1px solid #1e293b",  // slate-800 outline
                }}
              >
                <code style={{ color: "#f8fafc", background: "transparent" }}>{skill.code}</code>
              </pre>
            ) : (
              <div className="mt-1 text-gray-400 italic">No source available.</div>
            )
          )}
        </div>
      </div>
    </div>
  );
}

// ── Submitted standalone agent preview (system prompt + tools + skills) ──
// Shown for governance_approval items where entity_type === "agents" (Agent
// Builder). Reads the approver-only /governance/agents/{name}/config endpoint,
// scoped to the submitter with owner_id, and reuses AgentDetail. Fails silently
// (403/404) so it never blocks the action panel for non-approvers.
function AgentApprovalPreview({ meta }) {
  const [state, setState] = useState("loading"); // loading | ready | error
  const [cfg, setCfg] = useState(null);           // { name, description, instructions, tools, skills, ... }

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    setCfg(null);
    async function load() {
      try {
        const qs = meta.owner_id ? `?owner_id=${encodeURIComponent(meta.owner_id)}` : "";
        const r = await authFetch(`${API}/governance/agents/${encodeURIComponent(meta.entity_name)}/config${qs}`);
        if (cancelled) return;
        if (!r.ok) { setState("error"); return; }
        const d = await r.json();
        setCfg(d);
        setState("ready");
      } catch (_) {
        if (!cancelled) setState("error");
      }
    }
    if (meta?.entity_name) load(); else setState("error");
    return () => { cancelled = true; };
  }, [meta?.entity_name, meta?.owner_id]);

  if (state === "loading") {
    return <div className="mt-5 text-xs text-gray-400">Loading agent preview…</div>;
  }
  if (state === "error" || !cfg) {
    // Non-approvers get 403, wrong owner gets 404 — stay quiet, never block.
    return null;
  }

  return (
    <div className="mt-5 rounded-lg border border-gray-200 overflow-hidden">
      <div className="px-4 py-3 bg-gray-50 border-b border-gray-100">
        <div className="flex items-center gap-2 text-sm font-semibold text-gray-700">
          <Bot size={14} className="text-gray-500" />
          Submitted agent
        </div>
        {cfg.description && (
          <p className="text-xs text-gray-500 mt-1 leading-relaxed">{cfg.description}</p>
        )}
      </div>
      <div className="px-4 py-3">
        <AgentDetail data={cfg} />
      </div>
    </div>
  );
}

// ── Submitted workflow preview (summary + expandable graph) ──
// Shown for governance_approval items where entity_type === "workflows" so an
// approver can see what the workflow actually does before approving. Reads via
// the approver-only /governance/workflows/{name}/graph endpoint, scoped to the
// submitter with owner_id. Sits outside UniversalInboxActions so it renders
// regardless of approver rights / actionable status, and never blocks actions.
function WorkflowApprovalPreview({ meta }) {
  const [state, setState] = useState("loading"); // loading | ready | error
  const [graph, setGraph] = useState(null);       // { name, description, author, graphData }
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    setGraph(null);
    setExpanded(false);
    async function load() {
      try {
        const qs = meta.owner_id ? `?owner_id=${encodeURIComponent(meta.owner_id)}` : "";
        const r = await authFetch(`${API}/governance/workflows/${encodeURIComponent(meta.entity_name)}/graph${qs}`);
        if (cancelled) return;
        if (!r.ok) { setState("error"); return; }
        const d = await r.json();
        setGraph(d);
        setState("ready");
      } catch (_) {
        if (!cancelled) setState("error");
      }
    }
    if (meta?.entity_name) load(); else setState("error");
    return () => { cancelled = true; };
  }, [meta?.entity_name, meta?.owner_id]);

  if (state === "loading") {
    return (
      <div className="mt-5 text-xs text-gray-400">Loading workflow preview…</div>
    );
  }
  if (state === "error" || !graph) {
    // Non-approvers get 403 and wrong owner gets 404 — stay quiet, never block.
    return null;
  }

  const nodes = Array.isArray(graph.graphData?.nodes) ? graph.graphData.nodes : [];
  const edges = Array.isArray(graph.graphData?.edges) ? graph.graphData.edges : [];
  const startNode = nodes.find((n) => n.type === "start");

  return (
    <div className="mt-5 rounded-lg border border-gray-200 overflow-hidden">
      <div className="px-4 py-3 bg-gray-50 border-b border-gray-100">
        <div className="flex items-center gap-2 text-sm font-semibold text-gray-700">
          <GitBranch size={14} className="text-gray-500" />
          Submitted workflow
        </div>
        {graph.description && (
          <p className="text-xs text-gray-500 mt-1 leading-relaxed">{graph.description}</p>
        )}
        <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-xs text-gray-500">
          {graph.author && <span>Author: <span className="text-gray-700">{graph.author}</span></span>}
          <span>Nodes: <span className="text-gray-700">{nodes.length}</span></span>
          {startNode && (
            <span>Start: <span className="text-gray-700">{startNode.data?.label || startNode.data?.name || "start"}</span></span>
          )}
        </div>
      </div>

      {/* Node summary list */}
      {nodes.length > 0 && (
        <ul className="divide-y divide-gray-50">
          {nodes.map((n) => (
            <WorkflowNodeRow key={n.id} node={n} />
          ))}
        </ul>
      )}

      {/* Expandable read-only diagram */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-1.5 px-4 py-2.5 text-xs font-medium text-gray-600 hover:bg-gray-50 border-t border-gray-100 cursor-pointer"
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {expanded ? "Hide diagram" : "View diagram"}
      </button>
      {expanded && (
        <div style={{ height: 360 }} className="border-t border-gray-100 bg-white">
          <WorkflowPreview graphData={{ nodes, edges }} />
        </div>
      )}
    </div>
  );
}

// ── KB document download button ──────────────────────────────
// Shown only inside the kb_approval PENDING_APPROVAL action block.
// Calls GET /api/kb/original/<docId> — the endpoint streams the retained
// original binary (PDF/DOCX/etc.) with Content-Disposition: attachment so
// the browser saves it with the human-readable filename.
// The original file exists on disk from upload time until successful
// activation, so it is always available here — including after a parse
// failure rolls the doc back to PENDING_APPROVAL for retry.
function KbDocDownloadButton({ docId, filename }) {
  const [dlError, setDlError] = useState(null);
  const [dlBusy, setDlBusy]   = useState(false);

  async function handleDownload() {
    setDlError(null);
    setDlBusy(true);
    try {
      const res = await authFetch(`${API}/kb/original/${docId}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Download failed (${res.status})`);
      }
      // Stream the response into a Blob and trigger a browser save dialog.
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      a.download = filename || `document_${docId}`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      setDlError(err.message || "Download failed. Please try again.");
    } finally {
      setDlBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <button
        onClick={handleDownload}
        disabled={dlBusy}
        title="Download the original uploaded file to review before approving"
        className="flex items-center gap-1.5 px-4 py-2 cursor-pointer hover:bg-gray-100 text-gray-600 text-sm rounded border border-gray-100 font-medium disabled:opacity-50"
      >
        <Download size={13} />
        {dlBusy ? "Downloading…" : "Download"}
      </button>
      {dlError && (
        <span className="text-[11px] text-red-500">{dlError}</span>
      )}
    </div>
  );
}

// ── Universal approval action panel ──────────────────────────
function UniversalInboxActions({ item, me, onDone }) {
  const canAct = me?.can_approve === true;
  const [reason, setReason] = useState("");
  const [showReject, setShowReject] = useState(false);
  const [busy, setBusy] = useState(false);
  const [liveStatus, setLiveStatus] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  // actionDone: set after the current user takes an action — drives the confirmation banner
  const [actionDone, setActionDone] = useState(null); // { verb: "approved"|"rejected"|"promoted", by: email }
  const [actionError, setActionError] = useState(null);
  const meta = item?.metadata || {};
  // Priority: async-fetched liveStatus > current_status injected by pending-approvals endpoint > event-time meta.status
  const status = liveStatus ?? meta.current_status ?? meta.status;

  // Re-fetch the entity's current status each time the selected item changes.
  // This prevents showing Approve/Reject after someone else has already acted.
  useEffect(() => {
    setLiveStatus(null);
    setIsLoading(true);
    setReason("");
    setShowReject(false);
    setActionDone(null);
    setActionError(null);   // clear any previous error — never bleed across items
    if (!item?.id) return;

    let cancelled = false;

    async function fetchLiveStatus() {
      try {
        if (item.type === "governance_approval" && meta.entity_type && meta.entity_name) {
          const _govQs = meta.owner_id ? `?owner_id=${encodeURIComponent(meta.owner_id)}` : "";
          const r = await authFetch(`${API}/governance/${meta.entity_type}/${meta.entity_name}${_govQs}`);
          if (!cancelled && r.ok) {
            const d = await r.json();
            if (d?.status) setLiveStatus(d.status);
          }
        } else if (item.type === "kb_approval" && meta.entity_id) {
          const r = await authFetch(`${API}/kb/${meta.entity_id}`);
          if (!cancelled && r.ok) {
            const d = await r.json();
            if (d?.status) setLiveStatus(d.status);
          }
        } else if (item.type === "budget_request" && meta.request_id) {
          const r = await authFetch(`${API}/budget/requests/${meta.request_id}`);
          if (!cancelled && r.ok) {
            const d = await r.json();
            if (d?.status) setLiveStatus(d.status);
          }
        } else if (item.type === "product_approval" && meta.product_id) {
          const r = await authFetch(`${API}/products/${meta.product_id}`);
          if (!cancelled && r.ok) {
            const d = await r.json();
            if (d?.status) setLiveStatus(d.status);
          }
        }
      } catch (_) { /* live status optional — fall back to meta.status */ }
      if (!cancelled) setIsLoading(false);
    }

    fetchLiveStatus();
    return () => { cancelled = true; };
  }, [item?.id]);

  const _actorLabel = me?.email || "you";

  async function govAction(action) {
    // Client-side pre-check — mirrors reject_entity()'s inline
    // validate_free_text(reason) call in routers/governance_router.py
    // (via core/security_validation.py). The backend remains the
    // authoritative enforcer.
    if (action === "reject" && reason.trim()) {
      const reasonCheck = validateFreeText(reason);
      if (!reasonCheck.isValid) {
        setActionError(reasonCheck.errors[0]?.message || "Invalid reason");
        return;
      }
    }

    setBusy(true);
    setActionError(null);
    try {
      const body = action === "reject" ? JSON.stringify({ reason }) : undefined;
      const _govQs = meta.owner_id ? `?owner_id=${encodeURIComponent(meta.owner_id)}` : "";
      const res = await authFetch(
        `${API}/governance/${meta.entity_type}/${meta.entity_name}/${action}${_govQs}`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Request failed (${res.status})`);
      }
      const verb = action === "approve" ? "approved" : action === "reject" ? "rejected" : "promoted to Production";
      const nextStatus = action === "approve" ? "APPROVED" : action === "reject" ? "REJECTED" : "PRODUCTION";
      setLiveStatus(nextStatus);
      setActionDone({ verb, by: _actorLabel });
      onDone();
    } catch (err) {
      setActionError(err.message || "Action failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function kbAction(action) {
    setBusy(true);
    setActionError(null);
    try {
      const url = action === "reject"
        ? `${API}/kb/${meta.entity_id}/${action}?reason=${encodeURIComponent(reason)}`
        : `${API}/kb/${meta.entity_id}/${action}`;
      const res = await authFetch(url, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Request failed (${res.status})`);
      }
      const verb = action === "approve" ? "approved — parsing in progress" : "rejected";
      // Backend sets status=INDEXING on approve (kb_worker will advance to ACTIVE).
      // REJECTED stays as-is.
      const nextStatus = action === "approve" ? "INDEXING" : "REJECTED";
      setLiveStatus(nextStatus);
      setActionDone({ verb, by: _actorLabel });
      onDone();
    } catch (err) {
      setActionError(err.message || "Action failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function budgetAction(action) {
    setBusy(true);
    setActionError(null);
    try {
      const res = await authFetch(`${API}/budget/requests/${meta.request_id}/${action}`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Request failed (${res.status})`);
      }
      const verb = action === "approve" ? "approved" : "rejected";
      const nextStatus = action === "approve" ? "APPROVED" : "REJECTED";
      setLiveStatus(nextStatus);
      setActionDone({ verb, by: _actorLabel });
      onDone();
    } catch (err) {
      setActionError(err.message || "Action failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  // INDEXING is intentionally INCLUDED in RESOLVED_STATUSES — once a doc is approved
  // and the kb_worker is parsing it, no further action is needed from the approver.
  // Showing "already actioned" prevents double-approval attempts.
  // ACTIVE and APPROVED are both terminal-success states (APPROVED = legacy path).
  const RESOLVED_STATUSES = ["APPROVED", "INDEXING", "REJECTED", "PRODUCTION", "DEPRECATED", "ACTIVE", "approved", "rejected"];
  const isResolved = status && RESOLVED_STATUSES.includes(status);
  const isRejected = status === "REJECTED" || status === "rejected";
  // A governance approval item whose entity is back in DRAFT means the
  // submitter cancelled (withdrew) the deploy request. There's nothing left to
  // approve/reject, so instead of a blank panel we tell the approver it was
  // cancelled. This only applies to governance items — DRAFT is a normal
  // pre-submit state for other artifact kinds.
  const isCancelled = item?.type === "governance_approval" && status === "DRAFT";

  if (!canAct) return null;

  // Case 0: action failed — show error, keep buttons visible so user can retry
  if (actionError) {
    return (
      <div className="mt-6 pt-5 border-t border-gray-200 space-y-3">
        <div className="flex items-start gap-3 px-4 py-3 rounded-lg border bg-red-50 border-red-200 text-red-700 text-sm">
          <XCircle size={16} className="flex-shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <div className="font-medium">Action failed</div>
            <div className="text-xs mt-0.5 text-red-600">{actionError}</div>
          </div>
          <button onClick={() => setActionError(null)} className="text-red-400 hover:text-red-600 flex-shrink-0">
            <X size={13} />
          </button>
        </div>
      </div>
    );
  }

  // Case 1: current user JUST took an action — show confirmation banner
  // Note: INDEXING/ACTIVE progress is shown in the uploader's Request Status tab
  // in KnowledgeBase.jsx — not here. The Inbox action panel is approver-only;
  // the uploader (who needs the parsing progress) cannot see this component.
  if (actionDone) {
    return (
      <div className="mt-6 pt-5 border-t border-gray-200">
        <div className={`flex items-center gap-3 px-4 py-3 rounded-lg border text-sm font-medium
          ${isRejected
            ? "bg-red-50 border-red-200 text-red-700"
            : "bg-green-50 border-green-200 text-green-700"}`}>
          {isRejected
            ? <XCircle size={16} className="flex-shrink-0" />
            : <CheckCircle2 size={16} className="flex-shrink-0" />}
          <span>
            Request <strong>{actionDone.verb}</strong> by{" "}
            <strong>{actionDone.by}</strong>
          </span>
        </div>
      </div>
    );
  }

  // Case 2: someone else already actioned this before current user opened it
  if (isResolved) {
    const approvedBy = meta.approved_by || meta.actioned_by || "another approver";
    return (
      <div className="mt-6 pt-5 border-t border-gray-200">
        <div className={`flex items-center gap-3 px-4 py-3 rounded-lg border text-sm
          ${isRejected
            ? "bg-red-50 border-red-200 text-red-600"
            : "bg-gray-50 border-gray-200 text-gray-500"}`}>
          {isRejected
            ? <XCircle size={15} className="flex-shrink-0 text-red-500" />
            : <CheckCircle2 size={15} className="flex-shrink-0 text-green-500" />}
          <span>
            This request was already{" "}
            <strong className="text-gray-700">{status}</strong>
            {approvedBy !== "another approver" && (
              <> by <strong className="text-gray-700">{approvedBy}</strong></>
            )}
            {" "}— no further action needed.
          </span>
        </div>
      </div>
    );
  }

  // Case 2b: the submitter cancelled the deploy request — nothing to approve.
  if (isCancelled) {
    const cancelledBy = meta.withdrawn_by || meta.owner_name || meta.submitted_by || "the submitter";
    return (
      <div className="mt-6 pt-5 border-t border-gray-200">
        <div className="flex items-center gap-3 px-4 py-3 rounded-lg border text-sm bg-gray-50 border-gray-200 text-gray-500">
          <XCircle size={15} className="flex-shrink-0 text-gray-400" />
          <span>
            This deploy request was <strong className="text-gray-700">cancelled</strong>
            {cancelledBy !== "the submitter" && (
              <> by <strong className="text-gray-700">{cancelledBy}</strong></>
            )}
            {" "}— no further action needed.
          </span>
        </div>
      </div>
    );
  }

  // While fetching live status, don't show action buttons to prevent flicker
  if (isLoading) return null;

  // Governance entities: agents, skills, mcp, workflows
  if (item.type === "governance_approval") {
    // Default-deny: only show action buttons when status is explicitly actionable.
    // If status is unknown (fetch pending, missing metadata) → hide all buttons.
    const ACTIONABLE = new Set(["PENDING_APPROVAL", "PENDING_L2", "APPROVED"]);
    if (!status || !ACTIONABLE.has(status)) return null;
    // Maker-checker (separation of duties): the submitter cannot approve/reject
    // their own deploy request — even if they hold an approver role (admin/HOD).
    // The backend enforces this too (_require_scoped_approver), so this is a
    // UX guard that hides the buttons the server would reject anyway. Mirrors
    // the kb_approval block below. Shares the isSelfSubmission helper used to
    // suppress the Submitted-* preview blocks above.
    const isSelfApproval = isSelfSubmission(meta, me);
    return (
      <div className="mt-6 pt-5 border-t border-gray-200 space-y-3">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Actions</p>
        {isSelfApproval && status === "PENDING_APPROVAL" && (
          <div className="flex items-center gap-2 px-3 py-2 bg-yellow-50 border border-yellow-200 rounded text-xs text-yellow-700">
            <span>⚠ You submitted this request. A different approver must review it (separation of duties).</span>
          </div>
        )}
        <div className="flex gap-2 flex-wrap">
          {status === "PENDING_APPROVAL" && !isSelfApproval && (
            <>
              <button onClick={() => govAction("approve")} disabled={busy}
                className="px-4 py-2 cursor-pointer  brand-grad hover:opacity-70 text-white text-sm rounded font-medium disabled:opacity-50">
                ✓ Approve
              </button>
              <button onClick={() => setShowReject(v => !v)} disabled={busy}
                className="px-4 py-2 cursor-pointer  hover:bg-gray-100 text-gray-600 text-sm rounded font-medium border border-gray-100 disabled:opacity-50">
                ✗ Reject
              </button>
            </>
          )}
          {status === "APPROVED" && (
            <button onClick={() => govAction("promote")} disabled={busy}
              className="px-4 py-2 brand-grad hover:opacity-70 text-white text-sm rounded font-medium disabled:opacity-50 cursor-pointer">
              → Promote to Production
            </button>
          )}
        </div>
        {showReject && !isSelfApproval && (
          <RejectForm reason={reason} setReason={setReason}
            onConfirm={() => govAction("reject")} onCancel={() => setShowReject(false)} busy={busy} />
        )}
      </div>
    );
  }

  // KB document approval
  if (item.type === "kb_approval" && status === "PENDING_APPROVAL") {
    // Maker-checker: the user who uploaded the document cannot approve it
    const isSelfApproval = meta.uploaded_by && me?.email && meta.uploaded_by === me.email;

    return (
      <div className="mt-6 pt-5 border-t border-gray-200 space-y-3">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Actions</p>
        {isSelfApproval && (
          <div className="flex items-center gap-2 px-3 py-2 bg-yellow-50 border border-yellow-200 rounded text-xs text-yellow-700">
            <span>⚠ You uploaded this document. A different user must approve it (maker-checker policy).</span>
          </div>
        )}
        <div className="flex gap-2 flex-wrap">
          <button onClick={() => kbAction("approve")} disabled={busy || isSelfApproval}
            title={isSelfApproval ? "Maker-checker: you cannot approve your own upload" : ""}
            className="px-4 py-2 cursor-pointer brand-grad hover:opacity-70 text-white text-sm rounded font-medium disabled:opacity-50">
            ✓ Approve & Index
          </button>
          <button onClick={() => setShowReject(v => !v)} disabled={busy}
            className="px-4 py-2 cursor-pointer hover:bg-gray-100 text-gray-600 text-sm rounded border border-gray-100 font-medium disabled:opacity-50">
            ✗ Reject
          </button>
          {/* Download button — KB approval only. Fetches the original uploaded
              file (PDF/DOCX/etc.) from KB_DOC_STORAGE_PATH via the existing
              /api/kb/original/<doc_id> endpoint. The original is retained until
              activation succeeds, so it is always available here — including
              after a parse failure rolls the doc back to PENDING_APPROVAL. */}
          {meta.entity_id && (
            <KbDocDownloadButton docId={meta.entity_id} filename={meta.entity_name || meta.display_name || ""} />
          )}
        </div>
        {showReject && (
          <RejectForm reason={reason} setReason={setReason}
            onConfirm={() => kbAction("reject")} onCancel={() => setShowReject(false)} busy={busy} />
        )}
      </div>
    );
  }

  // Budget request approval
  if (item.type === "budget_request" && meta.request_id) {
    return (
      <div className="mt-6 pt-5 border-t border-gray-200 space-y-3">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Actions</p>
        <div className="flex gap-2">
          <button onClick={() => budgetAction("approve")} disabled={busy}
            className="px-4 py-2 cursor-pointer brand-grad hover:opacity-70 text-white text-sm rounded font-medium disabled:opacity-50">
            ✓ Approve Budget
          </button>
          <button onClick={() => budgetAction("reject")} disabled={busy}
            className="px-4 py-2 cursor-pointer hover:bg-gray-100 text-gray-600 text-sm rounded font-medium border border-gray-100 disabled:opacity-50">
            ✗ Reject
          </button>
        </div>
      </div>
    );
  }

  // Codebase index request approval
  if (item.type === "codebase_approval" && meta.request_id) {
    async function codebaseAction(action) {
      setBusy(true);
      setActionError(null);
      try {
        const url = action === "reject"
          ? `${API}/index/requests/${meta.request_id}/${action}`
          : `${API}/index/requests/${meta.request_id}/${action}`;
        const body = action === "reject" && reason
          ? JSON.stringify({ note: reason })
          : undefined;
        const res = await authFetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `Request failed (${res.status})`);
        }
        const verb = action === "approve" ? "approved — indexing started" : "rejected";
        const nextStatus = action === "approve" ? "approved" : "rejected";
        setLiveStatus(nextStatus);
        setActionDone({ verb, by: _actorLabel });
        onDone();
      } catch (err) {
        setActionError(err.message || "Action failed. Please try again.");
      } finally {
        setBusy(false);
      }
    }
    return (
      <div className="mt-6 pt-5 border-t border-gray-200 space-y-3">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Actions</p>
        <div className="text-xs text-gray-500 mb-1">
          Repo: <span className="font-medium text-gray-700">{meta.repo_name}</span>
          {meta.branch && <> · Branch: <span className="font-medium text-gray-700">{meta.branch}</span></>}
          {meta.submitted_by && <> · Requested by: <span className="font-medium text-gray-700">{meta.submitted_by}</span></>}
        </div>
        <div className="flex gap-2 flex-wrap">
          <button onClick={() => codebaseAction("approve")} disabled={busy}
            className="px-4 py-2  cursor-pointer brand-grad hover:opacity-70 text-white text-sm rounded font-medium disabled:opacity-50">
            ✓ Approve & Index
          </button>
          <button onClick={() => setShowReject(v => !v)} disabled={busy}
            className="px-4 py-2 cursor-pointer hover:bg-gray-100 text-gray-600 text-sm rounded font-medium border border-gray-100 disabled:opacity-50">
            ✗ Reject
          </button>
        </div>
        {showReject && (
          <RejectForm reason={reason} setReason={setReason}
            onConfirm={() => codebaseAction("reject")} onCancel={() => setShowReject(false)} busy={busy} />
        )}
      </div>
    );
  }

  // Product approval — approver-only action (ad_level ≤ 3)
  if (item.type === "product_approval" && meta.product_id && status !== "ACTIVE" && status !== "REJECTED") {
    async function productAction(action) {
      setBusy(true);
      setActionError(null);
      try {
        const url = action === "reject"
          ? `${API}/products/${meta.product_id}/${action}?note=${encodeURIComponent(reason)}`
          : `${API}/products/${meta.product_id}/${action}`;
        const res = await authFetch(url, { method: "POST" });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `Request failed (${res.status})`);
        }
        const verb = action === "approve" ? "approved" : "rejected";
        const nextStatus = action === "approve" ? "ACTIVE" : "REJECTED";
        setLiveStatus(nextStatus);
        setActionDone({ verb, by: _actorLabel });
        onDone();
      } catch (err) {
        setActionError(err.message || "Action failed. Please try again.");
      } finally {
        setBusy(false);
      }
    }
    return (
      <div className="mt-6 pt-5 border-t border-gray-200 space-y-3">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Actions</p>
        <div className="flex gap-2 flex-wrap">
          <button onClick={() => productAction("approve")} disabled={busy}
            className="px-4 py-2 brand-grad hover:opacity-70 text-white cursor-pointer text-sm rounded font-medium disabled:opacity-50">
            ✓ Approve Product
          </button>
          <button onClick={() => setShowReject(v => !v)} disabled={busy}
            className="px-4 py-2 hover:bg-gray-100 text-gray-600  border border-gray-100 text-sm rounded font-medium disabled:opacity-50 cursor-pointer">
            ✗ Reject
          </button>
        </div>
        {showReject && (
          <RejectForm reason={reason} setReason={setReason}
            onConfirm={() => productAction("reject")} onCancel={() => setShowReject(false)} busy={busy} />
        )}
      </div>
    );
  }

  return null;
}
