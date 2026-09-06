// SPDX-License-Identifier: MIT
// GovernanceReviewPanel — renders the GOVERNANCE_REPORT artifact for the
// GOVERNANCE_REVIEW stage (EA / IS / DPDP pluggable governance skills, run
// as ainxt-v2 CLI plugins over the diff — see agents/sdlc_governance/).
//
// Data source: GET /sdlc/runs/{runId}/governance  (or `report` prop, pre-fetched
// by the caller). Shape (agents/sdlc_governance/engine.py::render_report):
//   { overall_verdict: "PASS"|"FAIL", ref, iterations,
//     skills: [ { skill, verdict, summary, open, suppressed,
//                 findings: [ { skill, severity, file, rule, title, detail,
//                               fix_hint, snippet, line, status, fingerprint } ] } ],
//     report_md }
//
// "Mark false positive" POSTs a suppression row (product_id?, repo, skill,
// fingerprint, rule, reason) to /sdlc/governance-suppressions and optimistically
// flips that finding's status to "suppressed" (fingerprint-matched — mirrors
// agents/sdlc_governance/engine.py::apply_suppressions).
//
// Approval mode activates when run.state === "AWAITING_GOVERNANCE_APPROVAL". It fetches
// GET /sdlc/runs/{runId}/governance/findings and renders one of two boards (B4.3):
//   - Author triage board (owner/admin, before `governance_submitted_to_teams`):
//     per-finding Request-Fix / Mark-FP + "Send to governance teams".
//   - Team review board (per-domain approvers, once submitted to teams):
//     per-finding Accept / Send-back (mandatory comment) + comment thread;
//     domain Approve stays disabled until every visible finding is decisioned.

import { useState, useEffect } from "react";
import {
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Loader2,
  EyeOff,
  ChevronDown,
  ChevronUp,
  Clock,
  UserCheck,
  MessageSquare,
  Send,
  ThumbsUp,
  Download,
  GitMerge,
} from "lucide-react";
import { API_BASE as API, apiFetch } from "../../config";
import { useToast } from "../ui/DialogProvider.jsx";
import { usePermission } from "../../hooks/usePermission";
import { validateIdentifier, validateFreeText } from "../../utils/securityValidation";

// ---------------------------------------------------------------------------
// Badges
// ---------------------------------------------------------------------------

function VerdictBadge({ verdict }) {
  const pass = verdict === "PASS";
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
        pass
          ? "bg-green-100 text-green-700 border border-green-200"
          : "bg-red-100 text-red-700 border border-red-200"
      }`}
    >
      {pass ? <CheckCircle2 size={11} /> : <XCircle size={11} />}
      {verdict || "—"}
    </span>
  );
}

const SEVERITY_STYLES = {
  critical: "bg-red-100 text-red-700 border border-red-200",
  high:     "bg-orange-100 text-orange-700 border border-orange-200",
  medium:   "bg-amber-100 text-amber-700 border border-amber-200",
  low:      "bg-gray-100 text-gray-600 border border-gray-200",
};

function SeverityBadge({ severity }) {
  const key = (severity || "").toLowerCase();
  const cls = SEVERITY_STYLES[key] || SEVERITY_STYLES.low;
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide ${cls}`}>
      {severity || "unknown"}
    </span>
  );
}

const STATUS_STYLES = {
  open:       "bg-red-50 text-red-600 border border-red-200",
  fixed:      "bg-green-50 text-green-600 border border-green-200",
  suppressed: "bg-gray-100 text-gray-500 border border-gray-200",
};

function StatusChip({ status }) {
  const cls = STATUS_STYLES[status] || STATUS_STYLES.open;
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${cls}`}>
      {status || "open"}
    </span>
  );
}

const DOMAIN_STATUS_STYLES = {
  pending:           "bg-yellow-100 text-yellow-700 border border-yellow-200",
  approved:          "bg-green-100 text-green-700 border border-green-200",
  changes_requested: "bg-orange-100 text-orange-700 border border-orange-200",
};

const DOMAIN_STATUS_ICONS = {
  pending:           <Clock size={11} />,
  approved:          <CheckCircle2 size={11} />,
  changes_requested: <AlertTriangle size={11} />,
};

const DOMAIN_STATUS_LABELS = {
  pending:           "Pending",
  approved:          "Approved",
  changes_requested: "Changes Requested",
};

function DomainStatusChip({ status }) {
  const key = status || "pending";
  const cls = DOMAIN_STATUS_STYLES[key] || DOMAIN_STATUS_STYLES.pending;
  const icon = DOMAIN_STATUS_ICONS[key] || DOMAIN_STATUS_ICONS.pending;
  const label = DOMAIN_STATUS_LABELS[key] || key;
  return (
    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium ${cls}`}>
      {icon}
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Mark-false-positive control (read-only report mode)
// ---------------------------------------------------------------------------

function MarkFalsePositive({ finding, repo, productId, onSuppressed }) {
  const { toast } = useToast();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit() {
    // Client-side pre-check — mirrors validate_governance_suppression_request()
    // in core/security_validation.py: `reason` is free text via
    // validate_free_text(). The backend (POST /sdlc/governance-suppressions)
    // remains the authoritative enforcer.
    const trimmedReason = reason.trim();
    if (trimmedReason) {
      const reasonCheck = validateFreeText(trimmedReason);
      if (!reasonCheck.isValid) {
        toast.error(reasonCheck.errors[0]?.message || "Invalid reason");
        return;
      }
    }

    setLoading(true);
    try {
      const body = {
        repo,
        skill: finding.skill,
        fingerprint: finding.fingerprint,
        rule: finding.rule,
        reason: trimmedReason || undefined,
      };
      if (productId) body.product_id = productId;
      const res = await apiFetch(`${API}/sdlc/governance-suppressions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || "Failed to mark false positive");
        return;
      }
      toast.success("Marked as false positive — suppressed going forward");
      setOpen(false);
      onSuppressed && onSuppressed(finding);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLoading(false);
    }
  }

  if (finding.status !== "open") return null;

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-indigo-600 cursor-pointer"
        title="Suppress this finding for this repo (fingerprint-matched)"
      >
        <EyeOff size={11} /> Mark false positive
      </button>
    );
  }

  return (
    <div className="mt-1.5 flex items-center gap-1.5 flex-wrap">
      <input
        className="flex-1 min-w-[140px] border border-gray-200 rounded px-2 py-1 text-[11px] focus:outline-none focus:border-indigo-300"
        placeholder="Optional reason..."
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        autoFocus
      />
      <button
        onClick={submit}
        disabled={loading}
        className="flex items-center gap-1 px-2 py-1 bg-indigo-600 text-white text-[11px] rounded hover:bg-indigo-700 disabled:opacity-50 cursor-pointer"
      >
        {loading ? <Loader2 size={10} className="animate-spin" /> : null}
        Confirm
      </button>
      <button
        onClick={() => setOpen(false)}
        className="text-[11px] text-gray-400 hover:text-gray-600 cursor-pointer"
      >
        Cancel
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Finding row (read-only report mode)
// ---------------------------------------------------------------------------

function FindingRow({ finding, repo, productId, onSuppressed }) {
  const loc = finding.file
    ? `${finding.file}${finding.line ? `:${finding.line}` : ""}`
    : "";
  return (
    <div className="border border-gray-200 rounded-lg p-2.5 bg-white space-y-1">
      <div className="flex items-start gap-1.5 flex-wrap">
        <SeverityBadge severity={finding.severity} />
        <StatusChip status={finding.status} />
        {finding.rule && (
          <code className="text-[10px] text-gray-400 font-mono">{finding.rule}</code>
        )}
      </div>
      <p className="text-sm text-gray-800 font-medium">{finding.title || finding.rule || "Finding"}</p>
      {loc && <p className="text-[11px] text-gray-500 font-mono break-all">{loc}</p>}
      {finding.detail && <p className="text-xs text-gray-600">{finding.detail}</p>}
      {finding.fix_hint && (
        <p className="text-xs text-indigo-600">
          <span className="font-medium">Fix:</span> {finding.fix_hint}
        </p>
      )}
      {finding.snippet && (
        <pre className="text-[11px] bg-gray-50 border border-gray-100 rounded p-1.5 overflow-x-auto whitespace-pre-wrap text-gray-700">
          {finding.snippet}
        </pre>
      )}
      <MarkFalsePositive
        finding={finding}
        repo={repo}
        productId={productId}
        onSuppressed={onSuppressed}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-skill section (read-only report mode)
// ---------------------------------------------------------------------------

function SkillSection({ skill, repo, productId, onSuppressed }) {
  const findings = Array.isArray(skill.findings) ? skill.findings : [];
  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 bg-gray-50 border-b border-gray-200 flex-wrap">
        {skill.verdict === "PASS"
          ? <ShieldCheck size={14} className="text-green-500 flex-shrink-0" />
          : <ShieldAlert size={14} className="text-red-500 flex-shrink-0" />}
        <span className="font-semibold text-gray-800 text-sm uppercase">{skill.skill}</span>
        <VerdictBadge verdict={skill.verdict} />
        <span className="text-[10px] text-gray-400 ml-auto">
          {skill.open || 0} open · {skill.suppressed || 0} suppressed
        </span>
      </div>
      <div className="p-3 space-y-2">
        {skill.summary && <p className="text-xs text-gray-600 italic">{skill.summary}</p>}
        {findings.length === 0 ? (
          <p className="text-xs text-gray-400">No findings.</p>
        ) : (
          findings.map((f, i) => (
            <FindingRow
              key={f.fingerprint || i}
              finding={f}
              repo={repo}
              productId={productId}
              onSuppressed={onSuppressed}
            />
          ))
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Domain colour map (read-only report mode)
// ---------------------------------------------------------------------------

const DOMAIN_COLORS = {
  IS:     { bar: "border-indigo-400",  badge: "bg-indigo-50 text-indigo-700 border-indigo-200",   icon: "text-indigo-500"  },
  INFOSEC:{ bar: "border-indigo-400",  badge: "bg-indigo-50 text-indigo-700 border-indigo-200",   icon: "text-indigo-500"  },
  EA:     { bar: "border-violet-400",  badge: "bg-violet-50 text-violet-700 border-violet-200",   icon: "text-violet-500"  },
  DPDP:   { bar: "border-emerald-400", badge: "bg-emerald-50 text-emerald-700 border-emerald-200", icon: "text-emerald-500" },
};
const DOMAIN_COLORS_DEFAULT = { bar: "border-gray-300", badge: "bg-gray-100 text-gray-600 border-gray-200", icon: "text-gray-400" };

function domainColors(domain) {
  return DOMAIN_COLORS[(domain || "").toUpperCase()] || DOMAIN_COLORS_DEFAULT;
}

// Per-domain section wrapper (read-only report mode)
function DomainGroup({ domainKey, skills, repo, productId, onSuppressed }) {
  const label = domainKey || "Other";
  const colors = domainColors(domainKey);
  const allPass = skills.every((s) => s.verdict === "PASS");
  const openTotal = skills.reduce((n, s) => n + (s.open || 0), 0);
  const suppressedTotal = skills.reduce((n, s) => n + (s.suppressed || 0), 0);

  return (
    <div className={`border-l-4 ${colors.bar} pl-3 space-y-2`}>
      {/* Domain header row */}
      <div className="flex items-center gap-2 flex-wrap">
        {allPass
          ? <ShieldCheck size={13} className={`${colors.icon} flex-shrink-0`} />
          : <ShieldAlert size={13} className="text-red-500 flex-shrink-0" />}
        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-bold uppercase tracking-widest border ${colors.badge}`}>
          {label}
        </span>
        {!allPass && (
          <span className="text-[10px] text-red-500 font-medium">{openTotal} open</span>
        )}
        {allPass && (
          <span className="text-[10px] text-green-600 font-medium">All clear</span>
        )}
        {suppressedTotal > 0 && (
          <span className="text-[10px] text-gray-400">{suppressedTotal} suppressed</span>
        )}
      </div>
      {/* Per-skill cards within this domain */}
      {skills.map((sk) => (
        <SkillSection
          key={sk.skill}
          skill={sk}
          repo={repo}
          productId={productId}
          onSuppressed={onSuppressed}
        />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Loading / empty / error
// ---------------------------------------------------------------------------

function LoadingSkeleton() {
  return (
    <div className="space-y-3 animate-pulse">
      <div className="h-20 bg-gray-100 rounded-lg" />
      <div className="h-28 bg-gray-100 rounded-lg" />
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-gray-400">
      <ShieldCheck size={40} className="mb-3 opacity-30" />
      <p className="text-sm font-medium">No governance report yet</p>
      <p className="text-xs mt-1">Report appears once GOVERNANCE_REVIEW completes.</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Approval-mode shared helpers
// ---------------------------------------------------------------------------

function formatTimestamp(ts) {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

// ---------------------------------------------------------------------------
// Download helpers — export exactly what the caller can already see (the
// backend has already scoped `domains`/findings per-user: the author gets all
// domains, a domain approver only their own). So a client-side export of the
// loaded data inherently respects each user's visibility.
// ---------------------------------------------------------------------------

function _csvCell(v) {
  const s = v === null || v === undefined ? "" : String(v);
  // Quote every cell and escape embedded quotes so commas/newlines are safe.
  return `"${s.replace(/"/g, '""')}"`;
}

function buildFindingsCsv(domains) {
  const header = [
    "domain", "domain_status", "severity", "skill", "rule", "title",
    "file", "line", "disposition", "decision", "decision_comment",
    "detail", "fix_hint", "fingerprint",
  ];
  const rows = [header.map(_csvCell).join(",")];
  for (const d of domains || []) {
    for (const f of (d.findings || [])) {
      rows.push([
        d.domain,
        d.status,
        f.severity,
        f.skill,
        f.rule,
        f.title,
        f.file,
        f.line,
        normalizeDisposition(f),
        f.decision,
        f.decision_comment,
        f.detail,
        f.fix_hint,
        f.fingerprint,
      ].map(_csvCell).join(","));
    }
  }
  return rows.join("\r\n");
}

function buildReportCsv(report) {
  const header = [
    "domain", "skill", "verdict", "severity", "rule", "title",
    "file", "line", "status", "detail", "fix_hint", "fingerprint",
  ];
  const rows = [header.map(_csvCell).join(",")];
  for (const sk of (report?.skills || [])) {
    const findings = Array.isArray(sk.findings) ? sk.findings : [];
    if (findings.length === 0) {
      rows.push([sk.domain, sk.skill, sk.verdict, "", "", "", "", "", "", "", "", ""].map(_csvCell).join(","));
      continue;
    }
    for (const f of findings) {
      rows.push([
        sk.domain || f.domain, sk.skill, sk.verdict, f.severity, f.rule, f.title,
        f.file, f.line, f.status, f.detail, f.fix_hint, f.fingerprint,
      ].map(_csvCell).join(","));
    }
  }
  return rows.join("\r\n");
}

function triggerDownload(filename, content, mime = "text/plain;charset=utf-8") {
  try {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch {
    /* best-effort — a browser without Blob/URL support silently no-ops */
  }
}

// Defensive disposition reader — the findings API is expected to send
// `disposition` (open|author_fp|fix_requested|fix_confirmed|...), but older
// deployments may only send the legacy `status` (open|fixed|suppressed).
function normalizeDisposition(finding) {
  if (!finding) return "open";
  if (finding.disposition) return finding.disposition;
  if (finding.status === "fixed") return "fix_confirmed";
  if (finding.status === "suppressed") return "suppressed";
  if (finding.status) return finding.status;
  return "open";
}

// Team review board only ever acts on items still awaiting a human call.
// A finding that was just sent back stays visible (rendered with a "sent
// back" state) even though send_back moves its disposition to
// fix_requested behind the scenes — only genuinely fixed/suppressed items
// are hidden from teams.
const TEAM_VISIBLE_DISPOSITIONS = new Set(["open", "author_fp"]);
function isTeamVisible(finding) {
  if (finding?.decision === "send_back") return true;
  return TEAM_VISIBLE_DISPOSITIONS.has(normalizeDisposition(finding));
}

// Reads a run/context-level governance flag, checking run.context, the run
// object itself, and the findings-endpoint payload (in that order) so the
// flag works whichever layer the backend currently surfaces it from.
function getRunFlag(run, findingsMeta, key) {
  if (run?.context && run.context[key] !== undefined) return Boolean(run.context[key]);
  if (run && run[key] !== undefined) return Boolean(run[key]);
  if (findingsMeta && findingsMeta[key] !== undefined) return Boolean(findingsMeta[key]);
  return false;
}

const DISPOSITION_STYLES = {
  open:          "bg-red-50 text-red-600 border border-red-200",
  author_fp:     "bg-blue-50 text-blue-600 border border-blue-200",
  fix_requested: "bg-amber-50 text-amber-600 border border-amber-200",
  fix_confirmed: "bg-green-50 text-green-600 border border-green-200",
  accepted:      "bg-green-50 text-green-600 border border-green-200",
  send_back:     "bg-orange-50 text-orange-600 border border-orange-200",
  suppressed:    "bg-gray-100 text-gray-500 border border-gray-200",
};

function DispositionChip({ disposition }) {
  const key = disposition || "open";
  const cls = DISPOSITION_STYLES[key] || "bg-gray-100 text-gray-600 border border-gray-200";
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${cls}`}>
      {key.replace(/_/g, " ")}
    </span>
  );
}

function NotConvergingBanner() {
  return (
    <div className="flex items-start gap-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
      <AlertTriangle size={13} className="mt-0.5 flex-shrink-0" />
      <span>
        Automatic fixing couldn&apos;t resolve one or more findings, so they&apos;ve been reopened
        for you. You can edit the code and <strong>Request Fix</strong> again, <strong>Mark FP</strong>{" "}
        if it&apos;s a false positive, or leave them open and <strong>Send to governance teams</strong>{" "}
        for manual review.
      </span>
    </div>
  );
}

function GovernanceGateHeader({ domains, onResume, resuming, resumeError, canRatify }) {
  const approvedCount = domains.filter((d) => d.status === "approved").length;
  const totalCount = domains.length;
  const allApproved = totalCount > 0 && approvedCount === totalCount;

  return (
    <div>
      <div className="flex items-center gap-2 flex-wrap">
        {allApproved
          ? <ShieldCheck size={18} className="text-green-500" />
          : <ShieldAlert size={18} className="text-yellow-500" />}
        <h3 className="font-semibold text-gray-800">Governance Approval</h3>
        <span
          className={`text-xs font-medium px-2 py-0.5 rounded-full border ${
            allApproved
              ? "bg-green-100 text-green-700 border-green-200"
              : "bg-yellow-100 text-yellow-700 border-yellow-200"
          }`}
        >
          {approvedCount} of {totalCount} domain{totalCount !== 1 ? "s" : ""} approved
        </span>
      </div>
      {canRatify && allApproved && totalCount > 0 && (
        <div className="mt-3 space-y-1.5">
          <div className="flex items-center gap-3">
            <button
              onClick={onResume}
              disabled={resuming}
              title="All domains signed off — commit the approved governance fixes and cut the merge request"
              className="flex items-center gap-1.5 px-4 py-2 bg-green-600 text-white text-sm rounded-md hover:bg-green-700 disabled:opacity-50"
            >
              {resuming
                ? <><Loader2 size={13} className="animate-spin" /> Cutting MR…</>
                : <><GitMerge size={13} /> Ratify &amp; Cut MR</>}
            </button>
            {resumeError && <span className="text-red-600 text-xs">{resumeError}</span>}
          </div>
          <p className="text-[11px] text-gray-500">
            All domains approved — no further fixing. This commits the approved fixes to the
            governance-fix branch and opens the merge request.
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// (1) Author triage board — owner/admin, before submission to teams.
// ---------------------------------------------------------------------------

/**
 * Single finding row in the author triage board.
 * States: open/sent-back → [Mark for fix] [Mark FP]; marked (fix_requested) →
 * [✓ Fix requested] (toggle to unmark) [Mark FP]. "Mark for fix" only MARKS — the
 * fixer runs later via the board's single "Run fixes on all marked" button (no
 * auto-trigger). The marked state IS the selection — there is no separate checkbox.
 */
function AuthorFindingRow({ finding, runId, onRefresh, disabled }) {
  const { toast } = useToast();
  const [fpOpen, setFpOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [justification, setJustification] = useState("");
  const [loading, setLoading] = useState(false);

  const disposition = normalizeDisposition(finding);
  // A finding sent back by a governance team is actionable again on the author
  // board (re-mark / mark FP). A per-finding send-back re-opens its disposition to
  // 'open'; a domain-level send-back leaves the prior disposition intact.
  const sentBack = finding.decision === "send_back";
  // Marked-for-fix = disposition fix_requested, even if it still carries an old
  // send_back decision (so the author can fix what a team sent back).
  const isMarked = disposition === "fix_requested";
  const isOpen = !isMarked && (disposition === "open" || sentBack);
  const loc = finding.file
    ? `${finding.file}${finding.line ? `:${finding.line}` : ""}`
    : "";

  async function markForFix() {
    setLoading(true);
    try {
      const res = await apiFetch(
        `${API}/sdlc/runs/${runId}/governance/findings/${finding.fingerprint}/request-fix`,
        { method: "POST" }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || "Failed to mark for fix");
        return;
      }
      toast.success("Marked for fix — use 'Run fixes on all marked' to run the fixer");
      await onRefresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLoading(false);
    }
  }

  async function unmark() {
    setLoading(true);
    try {
      const res = await apiFetch(
        `${API}/sdlc/runs/${runId}/governance/findings/${finding.fingerprint}/unmark`,
        { method: "POST" }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || "Failed to unmark");
        return;
      }
      toast.success("Unmarked");
      await onRefresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLoading(false);
    }
  }

  // A false-positive justification is MANDATORY (server enforces 422 on blank).
  const canSubmitFp = justification.trim().length > 0;

  async function submitFp() {
    if (!canSubmitFp) return;

    // Client-side pre-check — mirrors validate_governance_decision_request()
    // in core/security_validation.py: `fp_justification` and `reason` are
    // both free text via validate_free_text(). The backend (POST
    // .../mark-fp) remains the authoritative enforcer.
    const trimmedJustification = justification.trim();
    const trimmedReason = reason.trim();
    const justificationCheck = validateFreeText(trimmedJustification);
    if (!justificationCheck.isValid) {
      toast.error(justificationCheck.errors[0]?.message || "Invalid justification");
      return;
    }
    if (trimmedReason) {
      const reasonCheck = validateFreeText(trimmedReason);
      if (!reasonCheck.isValid) {
        toast.error(reasonCheck.errors[0]?.message || "Invalid reason");
        return;
      }
    }

    setLoading(true);
    try {
      const res = await apiFetch(
        `${API}/sdlc/runs/${runId}/governance/findings/${finding.fingerprint}/mark-fp`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reason: trimmedReason || undefined,
            fp_justification: trimmedJustification,
          }),
        }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || "Failed to mark false positive");
        return;
      }
      toast.success("Marked as false positive");
      setFpOpen(false);
      await onRefresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="border border-gray-200 rounded-lg p-2.5 bg-white space-y-1.5">
      <div className="flex items-start gap-1.5 flex-wrap">
        <SeverityBadge severity={finding.severity} />
        <DispositionChip disposition={disposition} />
        {finding.skill && (
          <code className="text-[10px] text-gray-400 font-mono">{finding.skill}</code>
        )}
      </div>
      <p className="text-sm text-gray-800 font-medium">{finding.title || finding.rule || "Finding"}</p>
      {loc && <p className="text-[11px] text-gray-500 font-mono break-all">{loc}</p>}
      {finding.detail && <p className="text-xs text-gray-600">{finding.detail}</p>}
      {finding.fix_hint && (
        <p className="text-xs text-indigo-600">
          <span className="font-medium">Fix:</span> {finding.fix_hint}
        </p>
      )}
      {finding.snippet && (
        <pre className="text-[11px] bg-gray-50 border border-gray-100 rounded p-1.5 overflow-x-auto whitespace-pre-wrap text-gray-700">
          {finding.snippet}
        </pre>
      )}

      {sentBack && finding.decision_comment && (
        <div className="flex items-start gap-1.5 text-[11px] text-orange-700 bg-orange-50 border border-orange-200 rounded px-2 py-1.5">
          <Send size={11} className="mt-0.5 flex-shrink-0" />
          <span><span className="font-medium">Sent back by governance:</span> {finding.decision_comment}</span>
        </div>
      )}

      {isOpen && !fpOpen && (
        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={markForFix}
            disabled={loading || disabled}
            className="flex items-center gap-1.5 px-2.5 py-1 bg-indigo-100 text-indigo-700 border border-indigo-200 text-[11px] font-medium rounded hover:bg-indigo-200 disabled:opacity-50 cursor-pointer"
          >
            {loading ? <Loader2 size={10} className="animate-spin" /> : <AlertTriangle size={10} />}
            Mark for fix
          </button>
          <button
            onClick={() => setFpOpen(true)}
            disabled={loading || disabled}
            className="flex items-center gap-1.5 px-2.5 py-1 bg-white text-gray-700 border border-gray-300 text-[11px] font-medium rounded hover:bg-gray-100 disabled:opacity-50 cursor-pointer"
          >
            <EyeOff size={10} /> Mark FP
          </button>
        </div>
      )}

      {isMarked && !fpOpen && (
        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={unmark}
            disabled={loading || disabled}
            title="Marked for fix — click to unmark"
            className="flex items-center gap-1.5 px-2.5 py-1 bg-amber-600 text-white border border-amber-600 text-[11px] font-medium rounded hover:bg-amber-700 disabled:opacity-50 cursor-pointer"
          >
            {loading ? <Loader2 size={10} className="animate-spin" /> : <CheckCircle2 size={10} />}
            Fix requested
          </button>
          <button
            onClick={() => setFpOpen(true)}
            disabled={loading || disabled}
            className="flex items-center gap-1.5 px-2.5 py-1 bg-white text-gray-700 border border-gray-300 text-[11px] font-medium rounded hover:bg-gray-100 disabled:opacity-50 cursor-pointer"
          >
            <EyeOff size={10} /> Mark FP
          </button>
        </div>
      )}

      {(isOpen || isMarked) && fpOpen && (
        <div className="mt-1 space-y-1.5 border-t border-gray-100 pt-1.5">
          <input
            className="w-full border border-gray-200 rounded px-2 py-1 text-[11px] focus:outline-none focus:border-indigo-300"
            placeholder="Optional reason..."
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            autoFocus
          />
          <textarea
            className="w-full border border-gray-200 rounded px-2 py-1 text-[11px] focus:outline-none focus:border-indigo-300 resize-none"
            rows={2}
            placeholder="False-positive justification (required)..."
            value={justification}
            onChange={(e) => setJustification(e.target.value)}
          />
          {!canSubmitFp && (
            <p className="text-[10px] text-amber-600">
              A justification is required to mark a finding false positive.
            </p>
          )}
          <div className="flex items-center gap-2">
            <button
              onClick={submitFp}
              disabled={loading || !canSubmitFp}
              className="flex items-center gap-1 px-2.5 py-1 bg-indigo-600 text-white text-[11px] rounded hover:bg-indigo-700 disabled:opacity-50 cursor-pointer"
            >
              {loading ? <Loader2 size={10} className="animate-spin" /> : null}
              Confirm
            </button>
            <button
              onClick={() => setFpOpen(false)}
              className="text-[11px] text-gray-400 hover:text-gray-600 cursor-pointer"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Author triage board — shown to the run owner/admin while findings are
 * still being worked through the author remediation loop (not yet sent to
 * governance teams).
 */
function AuthorTriageBoard({ runId, domains, rescanning, onRefresh, submitted }) {
  const { toast } = useToast();
  const [submitting, setSubmitting] = useState(false);
  const [running, setRunning] = useState(false);

  // Domains a governance team has explicitly bounced back to the author, and/or
  // that carry a per-finding send-back. These need author action before re-send.
  const sentBackDomains = domains.filter(
    (d) => d.status === "changes_requested" ||
           (d.findings || []).some((f) => f.decision === "send_back")
  );
  // Post-submit the author only needs to act on what came back; pre-submit they
  // triage everything.
  const boardDomains = submitted ? sentBackDomains : domains;

  const allApproved = domains.length > 0 && domains.every((d) => d.status === "approved");
  const hasSentBack = sentBackDomains.length > 0;
  // Re-send only makes sense once a governance team has bounced something back.
  // After full approval (ratify is the next step) — or while teams are still
  // reviewing with nothing returned — there is nothing to re-send, so hide it.
  const showSubmit = !submitted || hasSentBack;
  // Banner copy + colour by state, so the author isn't told the run was "sent
  // back" when in fact everything is approved or still under review.
  const bannerMode = !submitted
    ? "triage"
    : hasSentBack
      ? "sentback"
      : allApproved
        ? "approved"
        : "reviewing";
  const BANNER = {
    triage:    { box: "bg-indigo-50 border-indigo-100", text: "text-indigo-700", icon: <UserCheck size={13} />,
                 msg: "Author triage — mark findings for fixing (or mark false positives), run the fixer on all marked, then send to governance teams." },
    sentback:  { box: "bg-orange-50 border-orange-200", text: "text-orange-700", icon: <Send size={13} />,
                 msg: "Governance teams have sent the domain(s) below back to you — address the findings (fix or mark false positive), then re-send to the teams." },
    approved:  { box: "bg-green-50 border-green-200", text: "text-green-700", icon: <ShieldCheck size={13} />,
                 msg: "All governance domains approved — use “Ratify & Cut MR” above to open the merge request." },
    reviewing: { box: "bg-gray-50 border-gray-200", text: "text-gray-600", icon: <Clock size={13} />,
                 msg: "Under governance review by the domain teams — nothing to act on right now." },
  }[bannerMode];

  // All fingerprints currently MARKED for fix (disposition fix_requested). The
  // marked state IS the selection — the fixer runs over ALL of these (no separate
  // subset checkbox). A finding a team sent back can be marked and fixed too, so
  // it is NOT excluded here even while it still carries an old send_back decision.
  const markedFps = [];
  for (const d of domains) {
    for (const f of (d.findings || [])) {
      if (normalizeDisposition(f) === "fix_requested") {
        markedFps.push(f.fingerprint);
      }
    }
  }

  async function runFixes(fingerprints) {
    setRunning(true);
    try {
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/governance/run-fixes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // fingerprints omitted → server fixes ALL currently marked findings.
        body: JSON.stringify(fingerprints ? { fingerprints } : {}),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || "Failed to start fixer");
        return;
      }
      const data = await res.json().catch(() => ({}));
      toast.success(`Fixer started for ${data.count ?? "the"} finding(s) — one batch run`);
      await onRefresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setRunning(false);
    }
  }

  async function submitToTeams() {
    setSubmitting(true);
    try {
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/governance/submit-to-teams`, {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || "Failed to submit to governance teams");
        return;
      }
      toast.success("Sent to governance teams for review");
      await onRefresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  const busy = rescanning || running;

  return (
    <div className="space-y-3">
      <div className={`flex items-center justify-between gap-2 flex-wrap border rounded-lg px-3 py-2 ${BANNER.box}`}>
        <div className={`flex items-center gap-2 text-xs ${BANNER.text}`}>
          {BANNER.icon}
          <span>{BANNER.msg}</span>
          {rescanning && (
            <span className="inline-flex items-center gap-1 text-indigo-500">
              <Loader2 size={11} className="animate-spin" /> fixing…
            </span>
          )}
        </div>
        {showSubmit && (
          <button
            onClick={submitToTeams}
            disabled={submitting || busy}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-indigo-600 text-white text-xs font-medium rounded hover:bg-indigo-700 disabled:opacity-50 cursor-pointer flex-shrink-0"
          >
            {submitting ? <Loader2 size={11} className="animate-spin" /> : <Send size={11} />}
            {submitted ? "Re-send to governance teams" : "Send to governance teams"}
          </button>
        )}
      </div>

      {/* Post-submit: compact per-domain status so the author can see which teams
          have acted (approved / pending / sent back). */}
      {submitted && (
        <div className="flex items-center gap-2 flex-wrap bg-white border border-gray-200 rounded-lg px-3 py-2">
          <span className="text-[11px] text-gray-500">Team status:</span>
          {domains.map((d) => (
            <span key={d.domain} className="inline-flex items-center gap-1">
              <span className="text-[11px] font-semibold text-gray-700 uppercase">{d.domain}</span>
              <DomainStatusChip status={d.status} />
            </span>
          ))}
        </div>
      )}

      {/* Batch fixer controls — one job / one CLI session handles the whole batch. */}
      <div className="flex items-center gap-2 flex-wrap bg-white border border-gray-200 rounded-lg px-3 py-2">
        <span className="text-[11px] text-gray-500">
          {markedFps.length} marked for fix
        </span>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => runFixes(null)}
            disabled={busy || markedFps.length === 0}
            title={markedFps.length === 0 ? "Mark one or more findings for fix first" : ""}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-600 text-white text-xs font-medium rounded hover:bg-amber-700 disabled:opacity-50 cursor-pointer"
          >
            {running ? <Loader2 size={11} className="animate-spin" /> : <CheckCircle2 size={11} />}
            Run fixes on all marked ({markedFps.length})
          </button>
        </div>
      </div>

      {submitted && !allApproved && boardDomains.length === 0 && (
        <div className="flex items-center gap-2 text-sm text-gray-500 p-3 border border-gray-200 rounded-lg bg-gray-50">
          <Clock size={14} />
          <span>Nothing to act on right now — governance teams are reviewing. You&apos;ll see any domain they send back here.</span>
        </div>
      )}

      {boardDomains.map((d) => {
        const findings = Array.isArray(d.findings) ? d.findings : [];
        const wasSentBack = d.status === "changes_requested" ||
          findings.some((f) => f.decision === "send_back");
        return (
          <div key={d.domain} className="border border-gray-200 rounded-lg overflow-hidden">
            <div className="flex items-center gap-2 px-3 py-2 bg-gray-50 border-b border-gray-200 flex-wrap">
              <span className="font-bold text-gray-800 text-sm uppercase tracking-wide">{d.domain}</span>
              {submitted && <DomainStatusChip status={d.status} />}
              <span className="text-[10px] text-gray-400 ml-auto">{findings.length} finding(s)</span>
            </div>
            {/* Team's domain-level send-back reason (stored as the approval note). */}
            {wasSentBack && d.note && (
              <div className="flex items-start gap-1.5 text-[11px] text-orange-700 bg-orange-50 border-b border-orange-200 px-3 py-2">
                <Send size={11} className="mt-0.5 flex-shrink-0" />
                <span>
                  <span className="font-medium">Sent back by {d.decided_by || `the ${d.domain} team`}:</span> {d.note}
                </span>
              </div>
            )}
            <div className="p-3 space-y-2">
              {findings.length === 0 ? (
                <p className="text-xs text-gray-400">No findings in this domain.</p>
              ) : (
                findings.map((f, i) => (
                  <AuthorFindingRow
                    key={f.fingerprint || i}
                    finding={f}
                    runId={runId}
                    onRefresh={onRefresh}
                    disabled={busy}
                  />
                ))
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// (2) Team review board — per-domain approvers, after submission to teams.
// ---------------------------------------------------------------------------

/**
 * Comment thread for a single finding. Prefers `finding.comments` if the
 * findings response already carries it; otherwise lazily fetches from the
 * per-finding comments endpoint (404 → treated as "no comments").
 */
function FindingComments({ runId, finding }) {
  const [open, setOpen] = useState(false);
  const [comments, setComments] = useState(Array.isArray(finding.comments) ? finding.comments : null);
  const [loading, setLoading] = useState(false);

  async function loadComments() {
    if (Array.isArray(finding.comments)) return;
    setLoading(true);
    try {
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/governance/findings/${finding.fingerprint}/comments`);
      if (res.status === 404) {
        setComments([]);
        return;
      }
      if (!res.ok) throw new Error(`comments ${res.status}`);
      const data = await res.json();
      setComments(Array.isArray(data) ? data : Array.isArray(data?.comments) ? data.comments : []);
    } catch {
      setComments([]);
    } finally {
      setLoading(false);
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && comments === null) loadComments();
  }

  const count = Array.isArray(finding.comments) ? finding.comments.length : null;

  return (
    <div>
      <button
        onClick={toggle}
        className="inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-indigo-600 cursor-pointer"
      >
        <MessageSquare size={11} /> {open ? "Hide comments" : "Comments"}
        {count ? ` (${count})` : ""}
      </button>
      {open && (
        <div className="mt-1.5 space-y-1">
          {loading ? (
            <p className="text-[11px] text-gray-400">Loading comments…</p>
          ) : comments && comments.length > 0 ? (
            comments.map((c, i) => (
              <div key={i} className="text-[11px] bg-gray-50 border border-gray-100 rounded px-2 py-1">
                <div className="flex items-center gap-1.5 text-gray-500 flex-wrap">
                  <span className="font-medium text-gray-700">{c.author_email || "unknown"}</span>
                  {c.role && <span className="text-gray-400">({c.role})</span>}
                  {c.created_at && <span className="text-gray-400 ml-auto">{formatTimestamp(c.created_at)}</span>}
                </div>
                {c.body && <p className="text-gray-600 mt-0.5">{c.body}</p>}
                {c.decision_context && (
                  <p className="text-gray-400 italic mt-0.5">{c.decision_context}</p>
                )}
              </div>
            ))
          ) : (
            <p className="text-[11px] text-gray-400">No comments yet.</p>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Single finding row in the team review board — Accept / Send-back
 * (send-back requires a mandatory comment) plus the comment thread.
 */
function TeamFindingRow({ runId, domain, finding, canApprove, onDecided }) {
  const { toast } = useToast();
  const [expanded, setExpanded] = useState(false);
  const [showSendBack, setShowSendBack] = useState(false);
  const [comment, setComment] = useState("");
  const [submitError, setSubmitError] = useState("");
  const [loading, setLoading] = useState(false);

  const disposition = normalizeDisposition(finding);
  const decided = Boolean(finding.decision);
  const loc = finding.file
    ? `${finding.file}${finding.line ? `:${finding.line}` : ""}`
    : "";
  const hasDetail = Boolean(finding.detail || finding.fix_hint || finding.snippet);
  const trimmedComment = comment.trim();

  async function submitDecision(decision) {
    if (decision === "send_back" && !trimmedComment) {
      setSubmitError("A comment is required to send back a finding.");
      return;
    }

    // Client-side pre-check — mirrors validate_governance_decision_request()
    // in core/security_validation.py: `comment` is free text via
    // validate_free_text(). The backend (POST .../decision) remains the
    // authoritative enforcer.
    if (trimmedComment) {
      const commentCheck = validateFreeText(trimmedComment);
      if (!commentCheck.isValid) {
        setSubmitError(commentCheck.errors[0]?.message || "Invalid comment");
        return;
      }
    }

    setLoading(true);
    setSubmitError("");
    try {
      const res = await apiFetch(
        `${API}/sdlc/runs/${runId}/governance/domains/${domain}/findings/${finding.fingerprint}/decision`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision, comment: trimmedComment || undefined }),
        }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const msg =
          err.detail ||
          (res.status === 422 ? "A comment is required to send back a finding." : "Failed to record decision");
        setSubmitError(msg);
        toast.error(msg);
        return;
      }
      toast.success(decision === "accept" ? "Finding accepted" : "Sent back for a fix");
      setShowSendBack(false);
      setComment("");
      await onDecided();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="border border-gray-200 rounded-lg p-2.5 bg-white space-y-1.5">
      <div className="flex items-start gap-1.5 flex-wrap">
        <SeverityBadge severity={finding.severity} />
        <DispositionChip disposition={disposition} />
        {finding.decision === "send_back" ? (
          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-orange-50 text-orange-600 border border-orange-200">
            <Send size={10} /> sent back — awaiting author
          </span>
        ) : (
          finding.decision && (
            <span className="text-[10px] text-gray-400">decision: {finding.decision}</span>
          )
        )}
        <span className="text-sm text-gray-800 font-medium flex-1 min-w-0 break-words">
          {finding.title || finding.rule || "Finding"}
        </span>
        {hasDetail && (
          <button
            onClick={() => setExpanded((v) => !v)}
            className="inline-flex items-center gap-0.5 text-[10px] text-gray-400 hover:text-gray-600 cursor-pointer flex-shrink-0"
          >
            {expanded ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
            {expanded ? "less" : "more"}
          </button>
        )}
      </div>
      {loc && <p className="text-[11px] text-gray-500 font-mono break-all">{loc}</p>}
      {finding.disposition === "author_fp" && finding.fp_justification && (
        <p className="text-xs text-gray-500">
          <span className="font-medium text-gray-600">Author FP justification:</span> {finding.fp_justification}
        </p>
      )}
      {finding.justification_required && (
        <p className="flex items-center gap-1 text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1">
          <AlertTriangle size={11} className="flex-shrink-0" />
          <span>Needs re-justification — this false positive has no justification and blocks approval.</span>
        </p>
      )}
      {decided && finding.decision_comment && (
        <p className="text-xs text-gray-500">
          <span className="font-medium text-gray-600">Reviewer comment:</span> {finding.decision_comment}
        </p>
      )}
      {expanded && (
        <div className="space-y-1">
          {finding.detail && <p className="text-xs text-gray-600">{finding.detail}</p>}
          {finding.fix_hint && (
            <p className="text-xs text-indigo-600">
              <span className="font-medium">Fix:</span> {finding.fix_hint}
            </p>
          )}
          {finding.snippet && (
            <pre className="text-[11px] bg-gray-50 border border-gray-100 rounded p-1.5 overflow-x-auto whitespace-pre-wrap text-gray-700">
              {finding.snippet}
            </pre>
          )}
        </div>
      )}

      <FindingComments runId={runId} finding={finding} />

      {decided && finding.decision === "send_back" && (
        <p className="text-[11px] text-orange-500 pt-1 border-t border-gray-100 flex items-center gap-1">
          <Send size={11} /> Sent back — awaiting author fix.
        </p>
      )}
      {canApprove && !decided && (
        <div className="pt-1 space-y-1.5 border-t border-gray-100">
          {showSendBack ? (
            <div className="space-y-1.5">
              <textarea
                className={`w-full border rounded px-2.5 py-1.5 text-xs focus:outline-none resize-none ${
                  !trimmedComment ? "border-red-200 focus:border-red-300" : "border-gray-200 focus:border-indigo-300"
                }`}
                rows={2}
                placeholder="Required: explain what needs to change…"
                value={comment}
                onChange={(e) => {
                  setComment(e.target.value);
                  setSubmitError("");
                }}
                autoFocus
              />
              {!trimmedComment && (
                <p className="text-[11px] text-red-500">A comment is required to send back a finding.</p>
              )}
              {submitError && trimmedComment && <p className="text-[11px] text-red-500">{submitError}</p>}
              <div className="flex items-center gap-2">
                <button
                  onClick={() => submitDecision("send_back")}
                  disabled={loading || !trimmedComment}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-orange-500 text-white text-xs font-medium rounded hover:bg-orange-600 disabled:opacity-50 cursor-pointer"
                >
                  {loading ? <Loader2 size={11} className="animate-spin" /> : <Send size={11} />}
                  Send back
                </button>
                <button
                  onClick={() => {
                    setShowSendBack(false);
                    setComment("");
                    setSubmitError("");
                  }}
                  className="text-xs text-gray-400 hover:text-gray-600 cursor-pointer"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 flex-wrap">
              <button
                onClick={() => submitDecision("accept")}
                disabled={loading}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 text-white text-xs font-medium rounded hover:bg-green-700 disabled:opacity-50 cursor-pointer"
              >
                {loading ? <Loader2 size={11} className="animate-spin" /> : <ThumbsUp size={11} />}
                Accept
              </button>
              <button
                onClick={() => setShowSendBack(true)}
                disabled={loading}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-orange-100 text-orange-700 border border-orange-200 text-xs font-medium rounded hover:bg-orange-200 disabled:opacity-50 cursor-pointer"
              >
                <Send size={11} /> Send back
              </button>
            </div>
          )}
        </div>
      )}
      {!canApprove && !decided && (
        <p className="text-[11px] text-gray-400 pt-1 border-t border-gray-100">
          Awaiting a domain approver decision.
        </p>
      )}
    </div>
  );
}

/**
 * Per-domain section of the team review board. The domain Approve button
 * stays disabled until every visible (open/author_fp) finding has a
 * recorded decision.
 */
function TeamDomainSection({ runId, domain, onRefresh }) {
  const { toast } = useToast();
  const [approving, setApproving] = useState(false);
  const [showDomainSendBack, setShowDomainSendBack] = useState(false);
  const [domainComment, setDomainComment] = useState("");
  const [sendingBack, setSendingBack] = useState(false);

  const canApproveDomain = Boolean(domain.can_approve);
  const isDecided = domain.status === "approved";
  const trimmedDomainComment = domainComment.trim();

  async function sendBackDomain() {
    if (!trimmedDomainComment) return;

    // Client-side pre-check — mirrors validate_governance_decision_request()
    // in core/security_validation.py: `comment` is free text via
    // validate_free_text(). The backend (POST .../send-back) remains the
    // authoritative enforcer.
    const commentCheck = validateFreeText(trimmedDomainComment);
    if (!commentCheck.isValid) {
      toast.error(commentCheck.errors[0]?.message || "Invalid comment");
      return;
    }

    setSendingBack(true);
    try {
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/governance/domains/${domain.domain}/send-back`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ comment: trimmedDomainComment }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || "Failed to send domain back to author");
        return;
      }
      toast.success(`${domain.domain} sent back to the author`);
      setShowDomainSendBack(false);
      setDomainComment("");
      await onRefresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setSendingBack(false);
    }
  }
  const allFindings = Array.isArray(domain.findings) ? domain.findings : [];
  const visibleFindings = allFindings.filter(isTeamVisible);
  // Unresolved = no decision recorded yet, OR already sent back (still
  // awaiting the author's fix). Approval must stay blocked in both cases —
  // the server enforces this too (409 send_back_pending); this is the
  // client-side mirror.
  const undecided = visibleFindings.filter((f) => !f.decision || f.decision === "send_back");
  // A domain with any un-justified FP (author_fp + no justification, e.g. a legacy
  // row) cannot be approved until the author supplies a justification. The server
  // enforces this too (409); this is the client-side mirror.
  const blockedByMissingJustification = Boolean(
    domain.blocked_by_missing_justification ||
    visibleFindings.some((f) => f.justification_required)
  );
  const canApproveNow =
    canApproveDomain && !isDecided && undecided.length === 0 && !blockedByMissingJustification;

  async function approveDomain() {
    setApproving(true);
    try {
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/governance/domains/${domain.domain}/approve`, {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const msg =
          err.detail ||
          (res.status === 409 ? "Some findings in this domain still need a decision." : "Failed to approve domain");
        toast.error(msg);
        return;
      }
      toast.success(`${domain.domain} domain approved`);
      await onRefresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setApproving(false);
    }
  }

  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      {/* Domain header */}
      <div className="flex items-center gap-2 px-3 py-2.5 bg-gray-50 border-b border-gray-200 flex-wrap">
        {domain.status === "approved"
          ? <ShieldCheck size={15} className="text-green-500 flex-shrink-0" />
          : <Clock size={15} className="text-yellow-500 flex-shrink-0" />}
        <span className="font-bold text-gray-800 text-sm uppercase tracking-wide">{domain.domain}</span>
        <DomainStatusChip status={domain.status} />
        {typeof domain.iteration === "number" && (
          <span className="text-[10px] text-gray-400">iteration {domain.iteration}</span>
        )}
        {domain.last_send_back_at && (
          <span className="text-[10px] text-gray-400">
            last send-back {formatTimestamp(domain.last_send_back_at)}
          </span>
        )}
        <span className="text-[10px] text-gray-400 ml-auto">{visibleFindings.length} to review</span>
      </div>

      {/* Findings list — only open/author_fp are visible to teams. A domain with
          nothing left to review (zero findings ever scanned, or every finding
          already resolved/suppressed) still needs an explicit team sign-off, so
          it renders as an approvable "all clear" card rather than a blank/empty
          section — the Approve button below is already enabled in this state
          (undecided.length === 0). */}
      <div className="p-3 space-y-2">
        {visibleFindings.length === 0 ? (
          <div className="flex items-center gap-2 text-xs text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2.5">
            <ShieldCheck size={14} className="text-green-500 flex-shrink-0" />
            <span>All clear — no issues found in this domain. Approve to acknowledge.</span>
          </div>
        ) : (
          visibleFindings.map((f, i) => (
            <TeamFindingRow
              key={f.fingerprint || i}
              runId={runId}
              domain={domain.domain}
              finding={f}
              canApprove={canApproveDomain}
              onDecided={onRefresh}
            />
          ))
        )}
      </div>

      {!canApproveDomain && domain.status === "pending" && (
        <div className="border-t border-gray-100 px-3 py-2 bg-gray-50 flex items-center gap-1.5 text-[11px] text-gray-400">
          <Clock size={11} />
          Awaiting {domain.domain} team approval
        </div>
      )}

      {canApproveDomain && !isDecided && (
        <div className="border-t border-gray-200 p-3 bg-gray-50 space-y-2">
          {domain.status === "changes_requested" && (
            <p className="text-[11px] text-orange-600 flex items-center gap-1">
              <Send size={11} /> This domain has been sent back to the author — awaiting their fixes and re-submission.
            </p>
          )}
          {showDomainSendBack ? (
            <div className="space-y-1.5">
              <textarea
                className={`w-full border rounded px-2.5 py-1.5 text-xs focus:outline-none resize-none ${
                  !trimmedDomainComment ? "border-red-200 focus:border-red-300" : "border-gray-200 focus:border-indigo-300"
                }`}
                rows={2}
                placeholder="Required: explain to the author what this domain needs before it can be approved…"
                value={domainComment}
                onChange={(e) => setDomainComment(e.target.value)}
                autoFocus
              />
              {!trimmedDomainComment && (
                <p className="text-[11px] text-red-500">A comment is required to send this domain back.</p>
              )}
              <div className="flex items-center gap-2">
                <button
                  onClick={sendBackDomain}
                  disabled={sendingBack || !trimmedDomainComment}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-orange-500 text-white text-xs font-medium rounded hover:bg-orange-600 disabled:opacity-50 cursor-pointer"
                >
                  {sendingBack ? <Loader2 size={11} className="animate-spin" /> : <Send size={11} />}
                  Send domain back to author
                </button>
                <button
                  onClick={() => { setShowDomainSendBack(false); setDomainComment(""); }}
                  className="text-xs text-gray-400 hover:text-gray-600 cursor-pointer"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 flex-wrap">
              <button
                onClick={approveDomain}
                disabled={!canApproveNow || approving}
                title={
                  blockedByMissingJustification
                    ? "A false positive needs a justification before this domain can be approved"
                    : !canApproveNow
                      ? `${undecided.length} of ${visibleFindings.length} finding(s) still unresolved (undecided or sent back)`
                      : ""
                }
                className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 text-white text-xs font-medium rounded hover:bg-green-700 disabled:opacity-50 cursor-pointer"
              >
                {approving ? <Loader2 size={11} className="animate-spin" /> : <CheckCircle2 size={11} />}
                Approve {domain.domain} domain
              </button>
              <button
                onClick={() => setShowDomainSendBack(true)}
                disabled={approving}
                title="Return this whole domain to the author with a reason"
                className="flex items-center gap-1.5 px-3 py-1.5 bg-orange-100 text-orange-700 border border-orange-200 text-xs font-medium rounded hover:bg-orange-200 disabled:opacity-50 cursor-pointer"
              >
                <Send size={11} /> Send back to author
              </button>
              {blockedByMissingJustification && (
                <span className="text-[11px] text-amber-600">
                  A false positive needs re-justification before this domain can be approved.
                </span>
              )}
              {!canApproveNow && !blockedByMissingJustification && (
                <span className="text-[11px] text-gray-400">
                  {undecided.length} of {visibleFindings.length} finding(s) still unresolved (undecided or sent back)
                </span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Team review board — shown once the run has been submitted to governance
 * teams. Each domain section gates its own Approve action server-side
 * (`can_approve`) and client-side (all visible findings decisioned).
 */
function TeamReviewBoard({ runId, domains, onRefresh, isAdmin }) {
  // Client-side mirror of the server's per-domain approver gate (defense in
  // depth only — the server is authoritative via auth.rbac.can_approve_domain).
  const visibleDomains = domains.filter((d) => d.can_approve || isAdmin);
  return (
    <div className="space-y-3">
      {visibleDomains.map((d) => (
        <TeamDomainSection key={d.domain} runId={runId} domain={d} onRefresh={onRefresh} />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Approval-mode container — decides author vs. team board and owns the
// shared findings fetch + gate-progress header.
// ---------------------------------------------------------------------------

/**
 * Full governance-approval panel — rendered when run.state === "AWAITING_GOVERNANCE_APPROVAL".
 * Splits into two purpose-built boards:
 *   - Author triage board: owner/admin, before `governance_submitted_to_teams`.
 *   - Team review board: per-domain approvers, once submitted to teams.
 */
function GovernanceApprovalPanel({ runId, run, onRefresh, user }) {
  const { isAdmin } = usePermission(user);
  const [domains, setDomains] = useState([]);
  const [findingsMeta, setFindingsMeta] = useState({});
  const [isOwnerServer, setIsOwnerServer] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // Resume state
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState('');
  // Cancel state
  const [cancelling, setCancelling] = useState(false);

  async function fetchFindings(opts = {}) {
    const silent = Boolean(opts.silent);
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    try {
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/governance/findings`);
      if (!res.ok) throw new Error(`findings ${res.status}`);
      const data = await res.json();
      setDomains(Array.isArray(data.domains) ? data.domains : []);
      setIsOwnerServer(Boolean(data.is_owner));
      setFindingsMeta(data || {});
    } catch (e) {
      if (!silent) setError(e?.message || "Failed to load governance findings");
    } finally {
      if (!silent) setLoading(false);
    }
  }

  useEffect(() => {
    if (runId) fetchFindings();
  }, [runId]);

  // Light poll so the author board's "re-scanning…" indicator and the team
  // board's decision state stay fresh without a manual refresh. Paused while
  // the tab is hidden and stopped once the run leaves the approval gate, so an
  // idle open tab no longer polls the findings endpoint indefinitely.
  useEffect(() => {
    if (!runId) return;
    if (run?.state !== "AWAITING_GOVERNANCE_APPROVAL") return;

    const tick = () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      fetchFindings({ silent: true });
    };
    const t = setInterval(tick, 8000);

    const onVisible = () => {
      if (document.visibilityState === "visible") fetchFindings({ silent: true });
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, run?.state]);

  async function handleResume() {
    setResuming(true);
    setResumeError('');
    try {
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/governance/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setResumeError(d.detail || `Resume failed: ${res.status}`);
      } else {
        if (typeof onRefresh === 'function') onRefresh();
      }
    } catch (err) {
      setResumeError(err.message);
    } finally {
      setResuming(false);
    }
  }

  if (loading) return <LoadingSkeleton />;
  if (error) {
    return (
      <div className="flex items-center gap-2 text-sm text-red-600 p-3 border border-red-200 rounded-lg bg-red-50">
        <AlertTriangle size={14} />
        <span>Failed to load governance findings: {error}</span>
      </div>
    );
  }
  if (domains.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-10 text-gray-400">
        <ShieldCheck size={36} className="mb-3 opacity-30" />
        <p className="text-sm font-medium">No domain findings available</p>
      </div>
    );
  }

  const submittedToTeams = getRunFlag(run, findingsMeta, "governance_submitted_to_teams");
  const notConverging = getRunFlag(run, findingsMeta, "governance_not_converging");
  // A batch fixer job is actually running iff the backend set governance_rescanning.
  // (Do NOT infer this from fix_requested dispositions — those now mean "marked, not
  // yet running" under the explicit-trigger model.)
  const rescanning = getRunFlag(run, findingsMeta, "governance_rescanning");
  const isOwnerOrAdmin = isOwnerServer || isAdmin;
  const isApprover = domains.some((d) => d.can_approve);

  // Role-based rendering (NOT a single mutually-exclusive mode): the run stays in
  // one state (AWAITING_GOVERNANCE_APPROVAL) for everyone; each user sees only the
  // content they own. The author (owner) ALWAYS gets their board — before
  // submission to triage, and after submission to see team progress + act on any
  // domain a team sends back (previously the global "submitted" flag flipped the
  // author to the team board, which was empty for them, so send-backs never
  // reached the author). Domain approvers get the team board once submitted.
  const showAuthor = isOwnerOrAdmin;
  const showTeam = submittedToTeams && (isApprover || isAdmin);
  const showWaiting = !showAuthor && !showTeam;

  const totalVisibleFindings = domains.reduce(
    (n, d) => n + (Array.isArray(d.findings) ? d.findings.length : 0), 0
  );

  function downloadIssues() {
    const csv = buildFindingsCsv(domains);
    triggerDownload(`governance-issues-${runId}.csv`, csv, "text/csv;charset=utf-8");
  }

  async function handleCancelRun() {
    if (typeof window !== "undefined" &&
        !window.confirm("Cancel this pipeline? This cannot be undone.")) {
      return;
    }
    setCancelling(true);
    try {
      const res = await apiFetch(`${API}/sdlc/runs/${runId}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "Cancelled by user", cancelled_by: "engineer" }),
      });
      if (res.ok && typeof onRefresh === "function") onRefresh();
    } catch (_e) {
      // best-effort; leave the panel state as-is on failure
    } finally {
      setCancelling(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-2 flex-wrap">
        <div className="flex-1 min-w-0">
          <GovernanceGateHeader domains={domains} onResume={handleResume} resuming={resuming} resumeError={resumeError} canRatify={showAuthor} />
        </div>
        <button
          onClick={downloadIssues}
          disabled={totalVisibleFindings === 0}
          title={totalVisibleFindings === 0 ? "No issues to download" : "Download the issues visible to you as CSV"}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-white text-gray-700 border border-gray-300 text-xs font-medium rounded hover:bg-gray-100 disabled:opacity-50 cursor-pointer flex-shrink-0"
        >
          <Download size={13} /> Download issues ({totalVisibleFindings})
        </button>
        {isOwnerOrAdmin && (
          <button
            onClick={handleCancelRun}
            disabled={cancelling}
            title="Cancel this pipeline"
            className="flex items-center gap-1.5 px-3 py-1.5 bg-white text-red-600 border border-red-300 text-xs font-medium rounded hover:bg-red-50 disabled:opacity-50 cursor-pointer flex-shrink-0"
          >
            {cancelling ? "Cancelling…" : "Cancel Pipeline"}
          </button>
        )}
      </div>
      {notConverging && <NotConvergingBanner />}

      {showAuthor && (
        <AuthorTriageBoard
          runId={runId}
          domains={domains}
          rescanning={rescanning}
          submitted={submittedToTeams}
          onRefresh={() => fetchFindings({ silent: true })}
        />
      )}
      {showTeam && (
        <TeamReviewBoard runId={runId} domains={domains} onRefresh={() => fetchFindings({ silent: true })} isAdmin={isAdmin} />
      )}
      {showWaiting && (
        <div className="flex items-center gap-2 text-sm text-gray-500 p-3 border border-gray-200 rounded-lg bg-gray-50">
          <Clock size={14} />
          <span>
            {submittedToTeams
              ? "This run is under governance review by the domain teams."
              : "Awaiting author triage before this run is sent to governance teams."}
          </span>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Read-only report panel (existing logic, extracted to avoid hooks-after-return)
// ---------------------------------------------------------------------------

function GovernanceReadOnlyPanel({ runId, repo, productId, reportProp }) {
  const [report, setReport] = useState(reportProp || null);
  const [loading, setLoading] = useState(!reportProp && Boolean(runId));
  const [error, setError] = useState(null);

  useEffect(() => {
    if (reportProp) {
      setReport(reportProp);
      setLoading(false);
      return;
    }
    if (!runId) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch(`${API}/sdlc/runs/${runId}/governance`)
      .then((r) => {
        if (!r.ok) throw new Error(`governance ${r.status}`);
        return r.json();
      })
      .then((data) => {
        if (cancelled) return;
        // GET /sdlc/runs/{id}/governance returns {run_id, report, created_at};
        // fall back to payload/raw for pre-fetched or differently-shaped inputs.
        setReport(data?.report ?? data?.payload ?? data ?? null);
      })
      .catch((e) => {
        if (!cancelled) setError(e?.message || "Failed to load governance report");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, reportProp]);

  // Optimistic update: flip a finding to "suppressed" by fingerprint match,
  // scoped to its owning skill (fingerprints are only unique within a skill).
  function handleSuppressed(finding) {
    setReport((prev) => {
      if (!prev) return prev;
      const skills = (prev.skills || []).map((sk) => {
        if (sk.skill !== finding.skill) return sk;
        const findings = (sk.findings || []).map((f) =>
          f.fingerprint === finding.fingerprint ? { ...f, status: "suppressed" } : f
        );
        const open = findings.filter((f) => f.status === "open").length;
        const suppressed = findings.filter((f) => f.status === "suppressed").length;
        return { ...sk, findings, open, suppressed, verdict: open ? "FAIL" : "PASS" };
      });
      const overall_verdict = skills.some((s) => s.verdict === "FAIL") ? "FAIL" : "PASS";
      return { ...prev, skills, overall_verdict };
    });
  }

  if (loading) return <LoadingSkeleton />;
  if (error) {
    return (
      <div className="flex items-center gap-2 text-sm text-red-600 p-3 border border-red-200 rounded-lg bg-red-50">
        <AlertTriangle size={14} />
        <span>Failed to load governance report: {error}</span>
      </div>
    );
  }
  if (!report) return <EmptyState />;

  const skills = Array.isArray(report.skills) ? report.skills : [];

  // Group skills by domain (mirrors engine.py::_render_md ordering).
  // Skills with no domain fall under a "" bucket rendered as "Other".
  const byDomain = {};
  for (const sk of skills) {
    const dom = (sk.domain || "").trim().toUpperCase();
    (byDomain[dom] = byDomain[dom] || []).push(sk);
  }
  const KNOWN_ORDER = ["IS", "INFOSEC", "EA", "DPDP"];
  const domainKeys = [
    ...KNOWN_ORDER.filter((d) => byDomain[d]),
    ...Object.keys(byDomain).filter((d) => d && !KNOWN_ORDER.includes(d)).sort(),
    ...(byDomain[""] ? [""] : []),
  ];

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center gap-2 flex-wrap">
        {report.overall_verdict === "PASS"
          ? <ShieldCheck size={18} className="text-green-500" />
          : <ShieldAlert size={18} className="text-red-500" />}
        <h3 className="font-semibold text-gray-800">Governance Review</h3>
        <VerdictBadge verdict={report.overall_verdict} />
        {report.ref && (
          <span className="text-[10px] text-gray-400 font-mono bg-gray-100 px-1.5 py-0.5 rounded">
            {report.ref}
          </span>
        )}
        {typeof report.iterations === "number" && (
          <span className="text-[10px] text-gray-400">{report.iterations} fix iteration(s)</span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {report.report_md && (
            <button
              onClick={() => triggerDownload(
                `governance-report-${runId || "run"}.md`,
                report.report_md, "text/markdown;charset=utf-8")}
              title="Download the full governance report (Markdown)"
              className="flex items-center gap-1.5 px-2.5 py-1 bg-white text-gray-700 border border-gray-300 text-[11px] font-medium rounded hover:bg-gray-100 cursor-pointer"
            >
              <Download size={12} /> Report (.md)
            </button>
          )}
          <button
            onClick={() => triggerDownload(
              `governance-report-${runId || "run"}.csv`,
              buildReportCsv(report), "text/csv;charset=utf-8")}
            disabled={skills.length === 0}
            title="Download the governance findings as CSV"
            className="flex items-center gap-1.5 px-2.5 py-1 bg-white text-gray-700 border border-gray-300 text-[11px] font-medium rounded hover:bg-gray-100 disabled:opacity-50 cursor-pointer"
          >
            <Download size={12} /> Issues (.csv)
          </button>
        </div>
      </div>

      {skills.length === 0 ? (
        <p className="text-sm text-gray-400 text-center py-6">
          No governance skills were evaluated for this run.
        </p>
      ) : (
        domainKeys.map((dk) => (
          <DomainGroup
            key={dk || "__other__"}
            domainKey={dk}
            skills={byDomain[dk]}
            repo={repo}
            productId={productId}
            onSuppressed={handleSuppressed}
          />
        ))
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

/**
 * GovernanceReviewPanel
 *
 * @param {{
 *   runId?: string,
 *   run?: { status?: string },
 *   repo?: string,
 *   productId?: string,
 *   report?: object,
 *   onRefresh?: () => void,
 *   user?: object
 * }} props
 *
 * When `run.state === "AWAITING_GOVERNANCE_APPROVAL"`, renders the
 * governance-approval container (GovernanceApprovalPanel), which itself
 * splits into the author triage board or the team review board. Otherwise
 * renders the existing read-only governance report (GovernanceReadOnlyPanel).
 *
 * The two modes are separate components so neither violates React's rules
 * of hooks — hooks are always called unconditionally within each component.
 */
export default function GovernanceReviewPanel({ runId, run, repo, productId, report: reportProp, onRefresh, user }) {
  if (run?.state === "AWAITING_GOVERNANCE_APPROVAL") {
    return <GovernanceApprovalPanel runId={runId} run={run} onRefresh={onRefresh} user={user} />;
  }
  return (
    <GovernanceReadOnlyPanel
      runId={runId}
      repo={repo}
      productId={productId}
      reportProp={reportProp}
    />
  );
}
