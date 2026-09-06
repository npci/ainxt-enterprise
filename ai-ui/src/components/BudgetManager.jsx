// SPDX-License-Identifier: MIT
import { useState, useEffect, useMemo, Fragment } from "react";
import { DollarSign, User, Users, AlertTriangle, CheckCircle2, XCircle, Clock, ChevronDown, ChevronRight, History, ArrowUpDown } from "lucide-react";

import { API_BASE as API, authFetch, apiFetch } from '../config';
import { toISTDate } from '../utils/time';
import { useConfirm } from "./ui/DialogProvider";
import { validateDescription } from '../utils/securityValidation';
import TeamBudgetPanel from './budget/TeamBudgetPanel';
import { SORT_DESC, sortByUtilisation, toggleSortDirection } from './budget/utilisationSort';
import { UtilizationPage } from './budget/UtilizationView';
import { utilizationEndpoints } from './budget/utilizationEndpoints';
import { decryptPii, encryptPii } from '../utils/piiCrypto';

// PII payload encryption flag (core/pii_crypto.py) — module-level singleton
// promise shared by every component in this file (fetched once, not once
// per component instance) since /auth/ui-config is unauthenticated and the
// flag never changes mid-session.
let _piiEnabledPromise = null;
function piiEnabled() {
  if (!_piiEnabledPromise) {
    _piiEnabledPromise = apiFetch(`${API}/auth/ui-config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => !!d?.pii_payload_encryption_enabled)
      .catch(() => false);
  }
  return _piiEnabledPromise;
}

// HOD-approval-hierarchy mode (core/config.py::HOD_APPROVAL_ENABLED). False
// (the default) means flat/admin-only — same module-level singleton-promise
// pattern as piiEnabled() above, sharing the same /auth/ui-config response
// shape. Independent of the (Tier-1-suppressed when this is false) per-user
// `user.is_hod` — this is a global deployment mode, not a per-user flag.
let _hodModePromise = null;
function hodModeEnabled() {
  if (!_hodModePromise) {
    _hodModePromise = apiFetch(`${API}/auth/ui-config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => !!d?.hod_approval_enabled)
      .catch(() => false);
  }
  return _hodModePromise;
}

// Platform "base" budget allocation (store.budget_store.BASE_COST_USD on the
// backend) — default $50, configurable via the BUDGET_BASE_COST_USD env var.
// Fetched once from /auth/ui-config (same singleton-promise pattern as the
// flags above) so the UI's copy always matches the deployed value instead of
// a hardcoded "$50".
let _baseCostUsdPromise = null;
function baseCostUsd() {
  if (!_baseCostUsdPromise) {
    _baseCostUsdPromise = apiFetch(`${API}/auth/ui-config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => Number(d?.budget_base_cost_usd ?? 50))
      .catch(() => 50);
  }
  return _baseCostUsdPromise;
}

// Fallback only — the real ceiling comes from GET /budget/me's
// `max_request_extra_usd` field, which mirrors the server-side
// BUDGET_MAX_INCREASE_PER_REQUEST_USD env var (default 200). Every usage
// below takes it as a `maxRequestExtraUsd` prop (or reads it directly off
// `myBudget`) and falls back to this constant only if the field hasn't
// loaded yet, so client-side validation/gating never drifts from what the
// server actually enforces. There is deliberately no separate hardcoded
// "hard platform ceiling" constant — BUDGET_MAX_INCREASE_PER_REQUEST_USD is
// the single source of truth for this limit everywhere in this file.
const DEFAULT_MAX_REQUEST_EXTRA_USD = 200;

// ── Card primitive used in "My Budget" (§7b) ───────────────────────────────
function StatCard({ label, value, sub, tone = "neutral", children, onClick }) {
  const toneCls = {
    neutral: "border-gray-200 bg-white",
    green:   "border-green-200 bg-green-50",
    yellow:  "border-yellow-200 bg-yellow-50",
    red:     "border-red-200 bg-red-50",
    indigo:  "border-indigo-200 bg-indigo-50",
  }[tone] || "border-gray-200 bg-white";
  const clickable = typeof onClick === "function";
  return (
    <div
      onClick={onClick}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={clickable ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); } } : undefined}
      className={`flex-1 min-w-[10rem] rounded-lg border ${toneCls} px-4 py-3 shadow-sm ${
        clickable ? "cursor-pointer hover:shadow-md hover:border-indigo-300 transition" : ""
      }`}
    >
      <div className="text-[11px] uppercase tracking-wide text-gray-500 font-medium flex items-center justify-between">
        <span>{label}</span>
        {clickable && <ChevronRight size={12} className="text-gray-400" />}
      </div>
      <div className="mt-1 text-xl font-semibold text-gray-800">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-gray-500">{sub}</div>}
      {children}
    </div>
  );
}

// ── Request Increase Modal (§2) ────────────────────────────────────────────
// Requested payload is EXTRA USD only, added on top of base once approved.
// Justification is MANDATORY. Success message names the HOD as the reviewer.
function RequestIncreaseModal({ currentBudget, hodEmail, hodName, delegateeEmails = [], onClose, user, budgetAtMax, hodMode = true, maxRequestExtraUsd }) {
  const [extra,      setExtra]      = useState(10.0);
  const [reason,     setReason]     = useState("");
  const [loading,    setLoading]    = useState(false);
  const [done,       setDone]       = useState(false);
  const [error,      setError]      = useState("");
  const [formErrors, setFormErrors] = useState({ extra: "", reason: "" });

  const maxExtra      = Number(maxRequestExtraUsd) > 0 ? Number(maxRequestExtraUsd) : DEFAULT_MAX_REQUEST_EXTRA_USD;
  const currentBase  = Number(currentBudget?.base_cost_usd  ?? 50);
  const currentExtra = Number(currentBudget?.extra_cost_usd ?? 0);
  const projected    = currentBase + currentExtra + (Number(extra) || 0);

  function validateField(name, value) {
    switch (name) {
      case "extra": {
        const n = Number(value);
        if (!value || Number.isNaN(n) || n <= 0) return "Extra amount must be greater than 0";
        if (n > maxExtra)                         return `Extra amount cannot exceed $${maxExtra.toLocaleString()} per request`;
        return "";
      }
      case "reason": {
        if (!value || !value.trim()) return "Justification is required";
        const r = validateDescription(value);
        if (!r.isValid) return r.errors[0]?.message || "Invalid justification";
        if (value.length > 1000) return "Justification must be ≤ 1000 characters";
        return "";
      }
      default: return "";
    }
  }

  async function submit() {
    const errs = {
      extra:  validateField("extra",  extra),
      reason: validateField("reason", reason),
    };
    if (Object.values(errs).some(Boolean)) { setFormErrors(errs); return; }
    setLoading(true); setError("");
    try {
      const r = await authFetch(`${API}/budget/request-increase`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id:                  user?.userId || "",
          requested_extra_cost_usd: Number(extra),
          justification:            reason.trim(),
        }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        const detail = d?.detail;
        if (Array.isArray(detail)) {
          setError(detail.map(e => e.msg || e.message || JSON.stringify(e)).join("; "));
        } else {
          setError(detail || `Failed (HTTP ${r.status})`);
        }
        return;
      }
      setDone(true);
    } finally { setLoading(false); }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md">
        {budgetAtMax ? (
          <div className="text-center py-4">
            <XCircle size={40} className="text-gray-400 mx-auto mb-3" />
            <h2 className="font-semibold text-gray-800 mb-1">Maximum extra amount reached</h2>
            <p className="text-sm text-gray-500 mb-4">
              You cannot request more than ${maxExtra.toLocaleString()} of extra budget in one request.
            </p>
            <button onClick={onClose} className="px-4 py-2 border border-gray-200 rounded text-sm hover:bg-gray-50 text-gray-700 cursor-pointer">Close</button>
          </div>
        ) : done ? (
          <div className="text-center py-4">
            <CheckCircle2 size={40} className="text-green-500 mx-auto mb-3" />
            <h2 className="font-semibold text-gray-800 mb-1">Request sent</h2>
            <p className="text-sm text-gray-500 mb-4">
              {hodMode ? (
                <>
                  Your request has been sent to{" "}
                  <b>{hodName || hodEmail || "your HOD"}</b>
                  {delegateeEmails.length > 0 && (
                    <> and {delegateeEmails.length} delegate{delegateeEmails.length === 1 ? "" : "s"}</>
                  )}{" "}for review.
                </>
              ) : (
                <>Your request has been sent to an admin for approval.</>
              )}{" "}You'll be notified in your inbox once it's approved or rejected.
            </p>
            <button onClick={onClose} className="px-4 py-2 border border-gray-200 rounded text-sm hover:bg-gray-50 text-gray-700">Close</button>
          </div>
        ) : (
          <>
            <h2 className="font-semibold text-gray-800 mb-1 flex items-center gap-2">
              <AlertTriangle size={16} className="text-yellow-500" /> Request Budget Increase
            </h2>
            <p className="text-xs text-gray-500 mb-3">
              Approved amounts are <b>added on top</b> of your base budget — they don't replace it.
            </p>
            {hodMode && (hodEmail || hodName) && (
              <div className="rounded-lg border border-indigo-100 bg-indigo-50 px-3 py-2 mb-3 text-xs text-indigo-800 flex items-start gap-2">
                <User size={13} className="shrink-0 text-indigo-400 mt-0.5" />
                <span>
                  Your request will be sent to{" "}
                  <b>{hodName || hodEmail}</b>
                  {hodName && hodEmail && <span className="text-indigo-500 ml-1">({hodEmail})</span>}
                  {" "}for approval.
                  {delegateeEmails.length > 0 && (
                    <>
                      {" "}Your HOD has also delegated approval to:{" "}
                      <b>{delegateeEmails.join(", ")}</b>. Any one of them may act on this request too.
                    </>
                  )}
                </span>
              </div>
            )}
            {!hodMode && (
              <div className="rounded-lg border border-indigo-100 bg-indigo-50 px-3 py-2 mb-3 text-xs text-indigo-800 flex items-start gap-2">
                <User size={13} className="shrink-0 text-indigo-400 mt-0.5" />
                <span>Your request will be sent to an admin for approval.</span>
              </div>
            )}
            <div className="space-y-3">
              <div>
                <label className="text-xs text-gray-500">Extra budget requested (USD) *</label>
                <input
                  type="number" step="0.5" min="0.01" max={maxExtra} value={extra}
                  onChange={e => { setExtra(e.target.value); if (formErrors.extra) setFormErrors(p => ({ ...p, extra: "" })); }}
                  onBlur={() => setFormErrors(p => ({ ...p, extra: validateField("extra", extra) }))}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none focus:border-indigo-300 ${formErrors.extra ? "border-red-500" : "border-gray-200"}`}
                />
                {formErrors.extra && <p className="mt-1 text-xs text-red-600">{formErrors.extra}</p>}
                <p className="mt-1 text-[11px] text-gray-500">
                  On approval, your budget will be <b>${projected.toFixed(2)}</b>.
                </p>
              </div>
              <div>
                <label className="text-xs text-gray-500">Justification *</label>
                <textarea
                  value={reason}
                  onChange={e => { setReason(e.target.value); if (formErrors.reason) setFormErrors(p => ({ ...p, reason: "" })); }}
                  onBlur={() => setFormErrors(p => ({ ...p, reason: validateField("reason", reason) }))}
                  rows={3}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none resize-none ${formErrors.reason ? "border-red-500" : "border-gray-200"}`}
                  placeholder={hodMode ? "Why do you need more budget? (this is shown to your HOD)" : "Why do you need more budget? (this is shown to the reviewing admin)"}
                />
                {formErrors.reason && <p className="mt-1 text-xs text-red-600">{formErrors.reason}</p>}
              </div>
              {error && <p className="text-xs text-red-600">{error}</p>}
              <div className="flex gap-2 pt-1">
                <button onClick={submit} disabled={loading}
                  className="px-4 py-2 bg-blue-600 text-white rounded text-sm brand-grad hover:opacity-70 cursor-pointer">
                  {loading ? "Sending…" : "Send Request"}
                </button>
                <button onClick={onClose} className="px-4 py-2 cursor-pointer border border-gray-200 rounded text-sm hover:bg-gray-100 text-gray-800 ">Cancel</button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── HOD / Admin Self-Increase Modal ─────────────────────────────────────────
// Shown instead of RequestIncreaseModal when the logged-in user is an HOD
// (HOD_APPROVAL_ENABLED=true) or, in flat/admin-only mode, an admin. Either
// way the increase is applied immediately (no approval flow). The HOD actor
// is charged against their own monthly allocation cap; the admin actor (flat
// mode) has no cap at all — admin judgment only, per plan.
function HodSelfIncreaseModal({ currentBudget, onClose, user, isAdminActor = false, maxRequestExtraUsd }) {
  const [extra,      setExtra]      = useState(10.0);
  const [loading,    setLoading]    = useState(false);
  const [capLoading, setCapLoading] = useState(!isAdminActor);
  const [done,       setDone]       = useState(false);
  const [error,      setError]      = useState("");
  const [formErrors, setFormErrors] = useState({ extra: "" });
  const [capStatus,  setCapStatus]  = useState(null);

  const maxExtraUsd  = Number(maxRequestExtraUsd) > 0 ? Number(maxRequestExtraUsd) : DEFAULT_MAX_REQUEST_EXTRA_USD;
  const currentBase  = Number(currentBudget?.base_cost_usd  ?? 50);
  const currentExtra = Number(currentBudget?.extra_cost_usd ?? 0);
  const projected    = currentBase + currentExtra + (Number(extra) || 0);

  // Admin actor (flat mode): no cap to check — remaining stays null.
  const remaining    = (!isAdminActor && capStatus) ? Number(capStatus.remaining_usd ?? 0) : null;
  const maxAllowed   = remaining !== null ? Math.min(maxExtraUsd, remaining) : maxExtraUsd;

  useEffect(() => {
    if (isAdminActor) return; // no cap status to load for the admin path
    setCapLoading(true);
    authFetch(`${API}/budget/hod/cap-status`)
      .then(r => r.json())
      .then(d => setCapStatus(d))
      .catch(() => setCapStatus(null))
      .finally(() => setCapLoading(false));
  }, [isAdminActor]);

  function validateExtra(value) {
    const n = Number(value);
    if (!value || Number.isNaN(n) || n <= 0) return "Amount must be greater than 0";
    if (n > maxExtraUsd)                      return `Amount cannot exceed $${maxExtraUsd} per request`;
    if (remaining !== null && n > remaining)  return `Amount exceeds your remaining HOD cap ($${remaining.toFixed(2)})`;
    return "";
  }

  async function submit() {
    const extraErr = validateExtra(extra);
    if (extraErr) { setFormErrors({ extra: extraErr }); return; }
    setLoading(true); setError("");
    try {
      const r = await authFetch(`${API}/budget/hod/self-increase`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id:                  user?.userId || "",
          requested_extra_cost_usd: Number(extra),
          justification:            isAdminActor ? "Admin self-increase" : "HOD self-increase",
        }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        const detail = d?.detail;
        setError(Array.isArray(detail) ? detail.map(e => e.msg || JSON.stringify(e)).join("; ") : detail || `Failed (HTTP ${r.status})`);
        return;
      }
      setDone(true);
    } finally { setLoading(false); }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md">
        {done ? (
          <div className="text-center py-4">
            <CheckCircle2 size={40} className="text-green-500 mx-auto mb-3" />
            <h2 className="font-semibold text-gray-800 mb-1">Budget increased</h2>
            <p className="text-sm text-gray-500 mb-4">
              {isAdminActor
                ? "Your budget limit has been increased immediately."
                : "Your budget limit has been increased immediately. The amount has been charged against your HOD cap."}
            </p>
            <button onClick={onClose} className="px-4 py-2 border border-gray-200 rounded text-sm hover:bg-gray-50 text-gray-700 cursor-pointer">Close</button>
          </div>
        ) : (
          <>
            <h2 className="font-semibold text-gray-800 mb-1 flex items-center gap-2">
              <AlertTriangle size={16} className="text-yellow-500" /> Increase Your Budget
            </h2>
            <p className="text-xs text-gray-500 mb-3">
              {isAdminActor
                ? <>As an admin, your budget increase is applied <b>immediately</b> — no approval required.</>
                : <>As an HOD, your budget increase is applied <b>immediately</b> — no approval required.
                   The amount will be deducted from your HOD monthly allocation cap.</>}
            </p>

            {/* HOD cap utilisation — admin actor has no cap, skip entirely */}
            {isAdminActor ? null : capLoading ? (
              <div className="text-xs text-gray-400 mb-3">Loading cap status…</div>
            ) : capStatus ? (
              <div className="rounded-lg border border-indigo-100 bg-indigo-50 px-4 py-3 mb-3 text-xs space-y-1">
                <div className="font-medium text-indigo-700 mb-1">Your HOD Cap — {capStatus.period_yyyymm}</div>
                <div className="flex justify-between text-gray-600">
                  <span>Monthly cap</span>
                  <span className="font-medium">${Number(capStatus.cap_usd ?? 0).toFixed(2)}</span>
                </div>
                <div className="flex justify-between text-gray-600">
                  <span>Consumed</span>
                  <span className="font-medium text-orange-600">${Number(capStatus.consumed_usd ?? 0).toFixed(2)}</span>
                </div>
                <div className="flex justify-between text-gray-700 font-semibold border-t border-indigo-200 pt-1 mt-1">
                  <span>Remaining</span>
                  <span className={remaining <= 0 ? "text-red-600" : "text-green-700"}>${(remaining ?? 0).toFixed(2)}</span>
                </div>
              </div>
            ) : (
              <div className="text-xs text-red-500 mb-3">Could not load cap status.</div>
            )}

            <div className="space-y-3">
              {remaining !== null && remaining <= 0 ? (
                <div className="text-sm text-red-600 font-medium">
                  You have no remaining HOD cap for this period. Budget cannot be increased.
                </div>
              ) : (
                <>
                  <div>
                    <label className="text-xs text-gray-500">
                      Extra budget (USD) * <span className="text-gray-400">— max ${maxAllowed.toFixed(2)}</span>
                    </label>
                    <input
                      type="number" step="0.5" min="0.01" max={maxAllowed} value={extra}
                      onChange={e => { setExtra(e.target.value); if (formErrors.extra) setFormErrors({ extra: "" }); }}
                      onBlur={() => setFormErrors({ extra: validateExtra(extra) })}
                      className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none focus:border-indigo-300 ${formErrors.extra ? "border-red-500" : "border-gray-200"}`}
                    />
                    {formErrors.extra && <p className="mt-1 text-xs text-red-600">{formErrors.extra}</p>}
                    <p className="mt-1 text-[11px] text-gray-500">
                      Current: base ${currentBase.toFixed(2)} + extra ${currentExtra.toFixed(2)}.
                      After increase, your total becomes <b>${projected.toFixed(2)}</b>.
                    </p>
                  </div>

                  <div className="rounded border border-yellow-200 bg-yellow-50 px-3 py-2 text-xs text-yellow-800">
                    This will <b>immediately increase</b> your budget limit without any approval.
                    {isAdminActor
                      ? ""
                      : ` $${Number(extra) > 0 ? Number(extra).toFixed(2) : "0.00"} will be deducted from your HOD cap.`}
                  </div>

                  {error && <p className="text-xs text-red-600">{error}</p>}
                </>
              )}
              <div className="flex gap-2 pt-1">
                {!(remaining !== null && remaining <= 0) && (
                  <button
                    onClick={submit}
                    disabled={loading || capLoading}
                    className="px-4 py-2 bg-blue-600 text-white rounded text-sm brand-grad hover:opacity-70 cursor-pointer disabled:opacity-50"
                  >
                    {loading ? "Applying…" : "Increase"}
                  </button>
                )}
                <button onClick={onClose} className="px-4 py-2 cursor-pointer border border-gray-200 rounded text-sm hover:bg-gray-100 text-gray-800">Cancel</button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Pending / Approved Requests Panel (§6 + §7 admin mixed view) ───────────
// Yellow cards for pending, green for approved. HOD sees only their own
// pending rows (server-scoped); admin sees pending + approved deduped by
// request_id. Approved rows show approver and justification.
export function PendingRequests({ isAdmin, isHod, onChange, scope = "", hodMode = true }) {
  const [requests, setRequests] = useState([]);
  const [loading,  setLoading]  = useState(true);
  const [acting,   setActing]   = useState(null); // request_id being acted on

  async function load() {
    setLoading(true);
    try {
      const qs = scope ? `?scope=${encodeURIComponent(scope)}` : "";
      const r = await authFetch(`${API}/budget/requests${qs}`);
      const d = await r.json();
      const piiOn = await piiEnabled();
      const decrypted = await Promise.all((d.requests || []).map(async req => ({
        ...req,
        hod_email:        await decryptPii(req.hod_email,        piiOn),
        requester_email:  await decryptPii(req.requester_email,  piiOn),
        requester_name:   await decryptPii(req.requester_name,   piiOn),
        approved_by:      await decryptPii(req.approved_by,      piiOn),
        approved_by_name: await decryptPii(req.approved_by_name, piiOn),
      })));
      setRequests(decrypted);
    } catch { /* ignore */ }
    setLoading(false);
  }
  useEffect(() => { load(); }, []);

  async function act(request_id, verb) {
    setActing(request_id);
    try {
      const r = await authFetch(`${API}/budget/requests/${encodeURIComponent(request_id)}/${verb}`, { method: "POST" });
      if (!r.ok) {
        // Live-status refresh on conflict — another HOD acted first.
        await load();
        return;
      }
      await load();
      onChange && onChange();
    } finally { setActing(null); }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-gray-400 py-6">
        <Clock size={13} className="animate-pulse" /> Loading requests…
      </div>
    );
  }
  if (!requests.length) {
    return (
      <div className="flex flex-col items-center justify-center h-40 text-gray-300">
        <CheckCircle2 size={32} className="mb-2" />
        <p className="text-sm">No {isAdmin ? "" : "pending "}requests</p>
      </div>
    );
  }

  // HODs may approve/reject — a user who is both admin and HOD can also act,
  // since they are the routed HOD for their department. In flat/admin-only
  // mode (!hodMode) admins are the routed approver pool instead, so any
  // admin viewing the admin requests tab may act too.
  const canAct = isHod || (isAdmin && !hodMode);

  return (
    <div className="space-y-3">
      {requests.map(req => {
        const status  = req.status || "pending";
        const yellow  = status === "pending";
        const green   = status === "approved";
        const red     = status === "rejected";
        const base    = Number(req.current_base_cost_usd  ?? 0);
        const extraNow = Number(req.current_extra_cost_usd ?? 0);
        const asked   = Number(req.requested_extra_cost_usd ?? req.requested_cost_usd ?? 0);
        const projected = base + extraNow + asked;
        const totalNow  = base + extraNow;
        const spent     = Number(req.usage_total?.cost_usd_spent ?? 0);
        const utilPct   = totalNow > 0 ? Math.min(100, Math.round((spent / totalNow) * 100)) : 0;
        const utilColor = utilPct >= 90 ? "bg-red-500" : utilPct >= 70 ? "bg-yellow-400" : "bg-green-500";
        return (
          <div key={req.request_id || req.id}
               className={`rounded-lg border p-4 ${
                 green  ? "border-green-200 bg-green-50" :
                 red    ? "border-red-200 bg-red-50" :
                 yellow ? "border-yellow-200 bg-yellow-50" :
                          "border-gray-200 bg-gray-50"
               }`}>
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  {yellow ? <Clock size={13} className="text-yellow-600" /> :
                   green  ? <CheckCircle2 size={13} className="text-green-600" /> :
                   red    ? <XCircle size={13} className="text-red-600" /> :
                            <XCircle size={13} className="text-gray-500" />}
                  <span className="text-sm font-medium text-gray-800 truncate">
                    {req.requester_name || req.requester_email || req.user_id}
                  </span>
                  {req.requester_department && (
                    <span className="text-[10px] text-gray-500 bg-white border border-gray-200 rounded px-1.5 py-0.5">
                      {req.requester_department}
                    </span>
                  )}
                  <span className="text-xs text-gray-400">
                    {req.created_at ? toISTDate(new Date(req.created_at * 1000)) : ""}
                  </span>
                  <span className={`ml-1 text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border font-medium ${
                    green  ? "text-green-700 border-green-300 bg-green-100" :
                    red    ? "text-red-700 border-red-300 bg-red-100" :
                    yellow ? "text-yellow-700 border-yellow-300 bg-yellow-100" :
                             "text-gray-600 border-gray-300 bg-gray-100"
                  }`}>{status}</span>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-xs text-gray-600 mt-1">
                  <div><span className="text-gray-400">Current base:</span> ${base.toFixed(2)}</div>
                  <div><span className="text-gray-400">Current extra:</span> ${extraNow.toFixed(2)}</div>
                  <div><span className="text-gray-400">Requested extra:</span> <b className="text-gray-800">${asked.toFixed(2)}</b></div>
                  <div><span className="text-gray-400">Resulting total:</span> <b className="text-gray-800">${projected.toFixed(2)}</b></div>
                </div>
                <div className="mt-2">
                  <div className="flex justify-between text-xs text-gray-600 mb-1">
                    <span className="text-gray-400">Current utilisation</span>
                    <span>{utilPct}% (${spent.toFixed(2)} / ${totalNow.toFixed(2)})</span>
                  </div>
                  <div className="h-1.5 bg-gray-200 rounded-full">
                    <div className={`h-1.5 rounded-full transition-all ${utilColor}`} style={{ width: `${utilPct}%` }} />
                  </div>
                </div>
                {req.justification && (
                  <p className="text-xs text-gray-700 mt-2 bg-white/60 border border-gray-200 rounded px-2 py-1.5 whitespace-pre-wrap">
                    <span className="text-gray-400">Justification: </span>{req.justification}
                  </p>
                )}
                {(isAdmin || isHod) && req.hod_email && (
                  <p className="text-[11px] mt-1.5 text-gray-500">
                    {hodMode ? <>Routed to HOD: <b>{req.hod_email}</b></> : <>Routed to admin: <b>{req.hod_email}</b></>}
                    {hodMode && (req.delegatees || []).length > 0 && (
                      <> · Delegated to: <b>{req.delegatees.join(", ")}</b></>
                    )}
                  </p>
                )}
                {(green || red) && (req.approved_by || req.approved_by_name) && (
                  <p className={`text-[11px] mt-1.5 ${red ? "text-red-600" : "text-gray-500"}`}>
                    {red ? "Rejected" : "Approved"} by <b>{req.approved_by_name || req.approved_by}</b>
                    {req.resolved_at && <> · {toISTDate(new Date(req.resolved_at))}</>}
                  </p>
                )}
              </div>
              {canAct && yellow && (
                <div className="flex-shrink-0 flex flex-col gap-1">
                  <button
                    disabled={acting === (req.request_id || req.id)}
                    onClick={() => act(req.request_id || req.id, "approve")}
                    className="px-3 py-1.5 text-xs bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50 cursor-pointer"
                  >Approve</button>
                  <button
                    disabled={acting === (req.request_id || req.id)}
                    onClick={() => act(req.request_id || req.id, "reject")}
                    className="px-3 py-1.5 text-xs bg-white text-red-700 border border-red-200 rounded hover:bg-red-50 disabled:opacity-50 cursor-pointer"
                  >Reject</button>
                </div>
              )}
            </div>
            {yellow && canAct && (
              <p className="text-[10px] text-gray-500 mt-2 border-t border-gray-200 pt-1.5">
                Approving <b>adds</b> ${asked.toFixed(2)} on top of the user's base budget — their base
                is left unchanged.
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── HOD budget-approval delegation panel (Team → Delegation) ───────────────
// Lets an HOD nominate one or more of their direct reports (resolved via
// org_tree) as approvers for budget-increase requests routed to them. Any
// nominated delegatee can approve/reject on the HOD's behalf — the charge
// always lands on the HOD's own monthly cap regardless of who acted.
function DelegationPanel() {
  const [directReports, setDirectReports] = useState([]);
  const [selected,      setSelected]      = useState(new Set());
  const [loading,       setLoading]       = useState(true);
  const [saving,        setSaving]        = useState(false);
  const [error,         setError]         = useState("");
  const [saved,         setSaved]         = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError("");
      try {
        const [reportsRes, delegatesRes] = await Promise.all([
          authFetch(`${API}/budget/hod/direct-reports`),
          authFetch(`${API}/budget/hod/delegates`),
        ]);
        const reportsData   = reportsRes.ok   ? await reportsRes.json()   : { direct_reports: [] };
        const delegatesData = delegatesRes.ok ? await delegatesRes.json() : { delegatee_emails: [] };
        if (cancelled) return;
        // Both endpoints may return "pii:v1:" ciphertext for the same
        // underlying email — since AES-GCM nonces are random, two
        // independently-encrypted copies of the same address never look
        // alike as strings. Decrypt to plaintext BEFORE building the
        // checkbox Set/matching, or pre-checks silently never match.
        const piiOn = await piiEnabled();
        const decryptedReports = await Promise.all(
          (reportsData.direct_reports || []).map(async r => ({
            ...r,
            email: await decryptPii(r.email, piiOn),
            name:  await decryptPii(r.name,  piiOn),
          }))
        );
        const decryptedDelegates = await Promise.all(
          (delegatesData.delegatee_emails || []).map(e => decryptPii(e, piiOn))
        );
        setDirectReports(decryptedReports);
        setSelected(new Set(decryptedDelegates.map(e => e.toLowerCase())));
      } catch {
        if (!cancelled) setError("Failed to load direct reports.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  function toggle(email) {
    setSaved(false);
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(email)) next.delete(email); else next.add(email);
      return next;
    });
  }

  async function save() {
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      const r = await authFetch(`${API}/budget/hod/delegates`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ delegatee_emails: Array.from(selected) }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        const detail = d?.detail;
        setError(Array.isArray(detail) ? detail.map(e => e.msg || JSON.stringify(e)).join("; ") : detail || `Failed (HTTP ${r.status})`);
        return;
      }
      setSaved(true);
    } catch (e) {
      setError(String(e?.message || e));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-gray-400 py-6">
        <Clock size={13} className="animate-pulse" /> Loading direct reports…
      </div>
    );
  }

  return (
    <div>
      <h2 className="text-sm font-semibold text-gray-700 mb-1">Delegate budget approval</h2>
      <p className="text-xs text-gray-500 mb-4">
        Nominate one or more of your direct reports to approve or reject budget-increase
        requests on your behalf. Any nominated delegatee can act — whoever acts first
        resolves the request. Approved amounts are always charged against{" "}
        <b>your</b> monthly allocation cap, never the delegatee's.
      </p>

      {!directReports.length ? (
        <div className="text-xs text-gray-400 italic bg-white border border-gray-200 rounded-lg px-4 py-3">
          No direct reports found for your account, so there is nobody to delegate to yet.
        </div>
      ) : (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden divide-y divide-gray-100">
          {directReports.map(r => (
            <label
              key={r.email}
              className="flex items-center gap-3 px-4 py-2.5 cursor-pointer hover:bg-gray-50"
            >
              <input
                type="checkbox"
                checked={selected.has(r.email)}
                onChange={() => toggle(r.email)}
                className="w-4 h-4 cursor-pointer accent-indigo-600"
              />
              <div className="min-w-0">
                <div className="text-sm text-gray-800 truncate">{r.name || r.email}</div>
                <div className="text-xs text-gray-400 truncate">{r.email}</div>
              </div>
            </label>
          ))}
        </div>
      )}

      {error && <p className="text-xs text-red-600 mt-3">{error}</p>}
      {saved && !error && (
        <p className="text-xs text-green-700 mt-3 flex items-center gap-1">
          <CheckCircle2 size={13} /> Delegation list saved.
        </p>
      )}

      <div className="mt-4">
        <button
          onClick={save}
          disabled={saving || !directReports.length}
          className="px-4 py-2 text-sm rounded text-white brand-grad hover:opacity-70 cursor-pointer disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save delegation"}
        </button>
      </div>
    </div>
  );
}

// ── HOD Allocation Audit / Increase-history table (§7c) ────────────────────
// Reads rows from GET /budget/admin/hod-audit — since §7 removes direct
// allocate/edit and only 'approve_request' rows remain, every row here is
// an increase event.
function HodAuditTable({ entries, rollup, loading, showHod = false }) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-gray-400 py-4 px-1">
        <Clock size={13} className="animate-pulse" /> Loading allocations…
      </div>
    );
  }
  if (!entries || !entries.length) {
    return (
      <div className="text-xs text-gray-400 italic py-3 px-1">
        No budget increases recorded this period.
      </div>
    );
  }

  const totals = Object.values(rollup || {}).reduce(
    (acc, v) => ({
      total: acc.total + (v.total_increased_usd || 0),
      count: acc.count + (v.allocation_count || 0),
      users: acc.users + (v.distinct_users || 0),
    }),
    { total: 0, count: 0, users: 0 },
  );

  return (
    <div>
      <div className="text-[11px] text-gray-500 mb-2">
        <strong className="text-gray-700">{totals.count}</strong> increase{totals.count !== 1 ? "s" : ""} ·{" "}
        <strong className="text-gray-700">{totals.users}</strong> user{totals.users !== 1 ? "s" : ""} ·{" "}
        <strong className="text-gray-700">${totals.total.toFixed(2)}</strong> total added on top of base
      </div>
      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-xs">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-3 py-2 text-left text-gray-500">When</th>
              {showHod && <th className="px-3 py-2 text-left text-gray-500">HOD</th>}
              <th className="px-3 py-2 text-left text-gray-500">User</th>
              <th className="px-3 py-2 text-right text-gray-500">Total before → after</th>
              <th className="px-3 py-2 text-right text-gray-500">Approved amount</th>
              <th className="px-3 py-2 text-left text-gray-500">Justification</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(e => (
              <tr key={e.id} className="border-t border-gray-100">
                <td className="px-3 py-2 text-gray-500 whitespace-nowrap">
                  {e.created_at ? toISTDate(new Date(e.created_at)) : "—"}
                </td>
                {showHod && (
                  <td className="px-3 py-2 text-gray-600 truncate max-w-[16ch]" title={e.hod_email}>
                    {e.hod_email}
                  </td>
                )}
                <td className="px-3 py-2">
                  <div className="text-gray-800 font-medium truncate max-w-[18ch]">
                    {e.target_name || e.target_email || e.target_user_id}
                  </div>
                  {e.target_email && e.target_name && (
                    <div className="text-[10px] text-gray-400 truncate max-w-[22ch]">{e.target_email}</div>
                  )}
                </td>
                <td className="px-3 py-2 text-right text-gray-600 whitespace-nowrap">
                  ${(e.previous_limit_usd ?? 0).toFixed(2)} <span className="text-gray-400">→</span> ${(e.new_limit_usd ?? 0).toFixed(2)}
                </td>
                <td className="px-3 py-2 text-right font-medium text-green-700 whitespace-nowrap">
                  +${(e.amount_usd || 0).toFixed(2)} added
                </td>
                <td className="px-3 py-2 text-gray-600 max-w-[26ch]">
                  {e.justification
                    ? <span title={e.justification} className="line-clamp-2">{e.justification}</span>
                    : <span className="text-gray-300">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── My Budget: own pending-request banner ──────────────────────────────────
// Shows the requester their own in-flight budget-increase request, in the
// same visual language as the admin/HOD pending queue (yellow card), but
// read-only — the requester cannot approve/reject their own request.
// Scoped server-side to `status='pending'` only: the moment it's approved
// or rejected it simply stops appearing here — approvals still show up in
// the "Budget increase history" table below, and rejections are notified
// via the inbox, per the existing budget_rejected notification flow.
const MY_PENDING_POLL_MS = 30_000; // periodic re-check so an approved/rejected
                                    // request drops off without a page reload

function MyPendingRequestCard({ refreshKey, onPendingChange, hodMode = true }) {
  const [requests, setRequests] = useState([]);
  const [loading,  setLoading]  = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load(showSpinner) {
      if (showSpinner) setLoading(true);
      try {
        const r = await authFetch(`${API}/budget/requests?scope=mine`);
        const d = r.ok ? await r.json() : { requests: [] };
        // hod_email is encrypt_pii()'d by GET /budget/requests (see
        // _encrypt_request_pii in routers/budget_router.py) and is rendered
        // directly in the "Awaiting approval from" line below, so it must be
        // decrypted here or the banner shows pii:v1: ciphertext.
        // `delegatees` is built separately in budget_store and is NOT in
        // _pii_keys, so it arrives as plaintext and is left alone.
        const piiOn = await piiEnabled();
        const list = await Promise.all((d.requests || []).map(async req => ({
          ...req,
          hod_email: await decryptPii(req.hod_email, piiOn),
        })));
        if (!cancelled) {
          setRequests(list);
          onPendingChange && onPendingChange(list.length > 0);
        }
      } catch {
        if (!cancelled) {
          setRequests([]);
          onPendingChange && onPendingChange(false);
        }
      }
      if (!cancelled && showSpinner) setLoading(false);
    }
    load(true);
    const timer = setInterval(() => load(false), MY_PENDING_POLL_MS);
    return () => { cancelled = true; clearInterval(timer); };
  }, [refreshKey]);

  if (loading || !requests.length) return null;

  return (
    <div className="space-y-3 mb-4">
      {requests.map(req => {
        const base      = Number(req.current_base_cost_usd  ?? 0);
        const extraNow  = Number(req.current_extra_cost_usd ?? 0);
        const asked     = Number(req.requested_extra_cost_usd ?? req.requested_cost_usd ?? 0);
        const projected = base + extraNow + asked;
        return (
          <div key={req.request_id || req.id} className="rounded-lg border border-yellow-200 bg-yellow-50 p-4">
            <div className="flex items-center gap-2 mb-1">
              <Clock size={13} className="text-yellow-600" />
              <span className="text-sm font-medium text-gray-800">Budget increase request pending</span>
              <span className="text-xs text-gray-400">
                {req.created_at ? toISTDate(new Date(req.created_at * 1000)) : ""}
              </span>
              <span className="ml-1 text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border font-medium text-yellow-700 border-yellow-300 bg-yellow-100">
                pending
              </span>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-xs text-gray-600 mt-1">
              <div><span className="text-gray-400">Current base:</span> ${base.toFixed(2)}</div>
              <div><span className="text-gray-400">Current extra:</span> ${extraNow.toFixed(2)}</div>
              <div><span className="text-gray-400">Requested extra:</span> <b className="text-gray-800">${asked.toFixed(2)}</b></div>
              <div><span className="text-gray-400">Resulting total:</span> <b className="text-gray-800">${projected.toFixed(2)}</b></div>
            </div>
            {req.justification && (
              <p className="text-xs text-gray-700 mt-2 bg-white/60 border border-gray-200 rounded px-2 py-1.5 whitespace-pre-wrap">
                <span className="text-gray-400">Justification: </span>{req.justification}
              </p>
            )}
            {req.hod_email && (
              <p className="text-[11px] mt-1.5 text-gray-500">
                {hodMode ? (
                  <>
                    Awaiting approval from: <b>{req.hod_email}</b>
                    {(req.delegatees || []).length > 0 && (
                      <> (delegated to: <b>{req.delegatees.join(", ")}</b>)</>
                    )}
                  </>
                ) : (
                  <>Awaiting approval from an admin.</>
                )}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── My Budget: user's own approved-increase audit trail (§7c) ──────────────
function MyIncreasesSection({ userId }) {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    (async () => {
      try {
        const r = await authFetch(`${API}/budget/my-increases`);
        const d = r.ok ? await r.json() : { entries: [] };
        const piiOn = await piiEnabled();
        const decrypted = await Promise.all((d.entries || []).map(async e => ({
          ...e,
          hod_email:        await decryptPii(e.hod_email,        piiOn),
          approved_by:      await decryptPii(e.approved_by,      piiOn),
          approved_by_name: await decryptPii(e.approved_by_name, piiOn),
        })));
        setEntries(decrypted);
      } catch { /* ignore */ }
      setLoading(false);
    })();
  }, [userId]);

  if (loading) return null;
  return (
    <div className="mt-6">
      <h3 className="text-xs font-medium text-gray-500 mb-2 uppercase tracking-wide flex items-center gap-1.5">
        <History size={12} /> Budget increase history
      </h3>
      {!entries.length ? (
        <div className="bg-white border border-gray-200 rounded-lg px-4 py-3 text-xs text-gray-400 italic">
          No approved budget increases yet. When your HOD approves a request, it will appear here.
        </div>
      ) : (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left text-gray-500">When</th>
                <th className="px-3 py-2 text-left text-gray-500">Approved by</th>
                <th className="px-3 py-2 text-right text-gray-500">Amount added</th>
                <th className="px-3 py-2 text-left text-gray-500">Justification (you submitted)</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(e => (
                <tr key={e.id} className="border-t border-gray-100">
                  <td className="px-3 py-2 text-gray-500 whitespace-nowrap">
                    {e.resolved_at ? toISTDate(new Date(e.resolved_at)) : "—"}
                  </td>
                  <td className="px-3 py-2 text-gray-700 truncate max-w-[20ch]" title={e.approved_by}>
                    {e.approved_by_name || e.approved_by || "—"}
                  </td>
                  <td className="px-3 py-2 text-right font-medium text-green-700 whitespace-nowrap">
                    +${(e.amount_usd || 0).toFixed(2)}
                  </td>
                  <td className="px-3 py-2 text-gray-600 max-w-[30ch]">
                    {e.justification
                      ? <span title={e.justification} className="line-clamp-2">{e.justification}</span>
                      : <span className="text-gray-300">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Team → Delegated: pending requests + HOD cap status for a delegatee ────
// Rendered for ANY user (HOD or not) who has been nominated as a delegatee
// by at least one HOD — lives under Team → Delegated rather than My Budget
// so it sits alongside the HOD's own Team → Pending Requests / Delegation
// tabs instead of cluttering the personal budget page. Shows:
//   1. The cap status of every HOD who delegated to them (read-only —
//      spending still comes out of the HOD's cap, not the delegatee's own).
//   2. The pending requests routed to them as a delegatee, with the same
//      Approve/Reject affordance HODs get on Team → Pending Requests.
function TeamDelegatedPanel() {
  const [delegatingHods, setDelegatingHods] = useState(null); // null = loading

  async function loadDelegatingHods() {
    try {
      const r = await authFetch(`${API}/budget/delegate/cap-status`);
      const d = r.ok ? await r.json() : { delegating_hods: [] };
      const piiOn = await piiEnabled();
      const decrypted = await Promise.all((d.delegating_hods || []).map(async h => ({
        ...h,
        hod_email: await decryptPii(h.hod_email, piiOn),
      })));
      setDelegatingHods(decrypted);
    } catch {
      setDelegatingHods([]);
    }
  }

  useEffect(() => { loadDelegatingHods(); }, []);

  if (delegatingHods === null) {
    return (
      <div className="flex items-center gap-2 text-xs text-gray-400 py-6">
        <Clock size={13} className="animate-pulse" /> Loading delegated approvals…
      </div>
    );
  }

  if (!delegatingHods.length) {
    return (
      <div className="flex flex-col items-center justify-center h-40 text-gray-300">
        <Users size={32} className="mb-2" />
        <p className="text-sm">No HOD has delegated budget approval to you.</p>
      </div>
    );
  }

  return (
    <div>
      <h2 className="text-sm font-semibold text-gray-700 mb-1">Delegated budget approval</h2>
      <p className="text-xs text-gray-500 mb-4">
        You've been nominated to approve budget-increase requests on behalf of the following HOD
        {delegatingHods.length === 1 ? "" : "s"}. Approving charges their cap, not yours.
      </p>

      <div className="space-y-2 mb-6">
        {delegatingHods.map(h => {
          const cap   = Number(h.cap_usd || 0);
          const used  = Number(h.consumed_usd || 0);
          const pct   = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
          const color = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-yellow-400" : "bg-green-500";
          return (
            <div key={h.hod_email} className="bg-white border border-gray-200 rounded-lg px-4 py-3">
              <div className="flex items-baseline justify-between mb-1">
                <span className="text-xs font-medium text-gray-700">{h.hod_email}</span>
                <span className="text-[11px] text-gray-500">Resets {h.resets_on}</span>
              </div>
              <div className="flex justify-between text-[11px] text-gray-600 mb-1">
                <span>${used.toFixed(2)} used · ${Number(h.remaining_usd || 0).toFixed(2)} remaining</span>
                <span className="font-medium text-gray-700">${used.toFixed(2)} / ${cap.toFixed(2)} ({pct}%)</span>
              </div>
              <div className="h-1.5 bg-gray-200 rounded-full">
                <div className={`h-1.5 rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
              </div>
            </div>
          );
        })}
      </div>

      <h3 className="text-xs font-medium text-gray-500 mb-2 uppercase tracking-wide">
        Pending requests routed to you
      </h3>
      <PendingRequests isAdmin={false} isHod={true} scope="delegate" onChange={loadDelegatingHods} />
    </div>
  );
}

// ── Org-Wide Monthly Cap (flat/admin-only mode) ─────────────────────────────
// Admin-only control for the single global SDLC/endpoint-spend ceiling
// (services/org_budget_governor.py, ainxt.org_wide_budget_cap). Shown
// instead of the per-department "HOD Caps" tab when HOD_APPROVAL_ENABLED is
// false — there's no department hierarchy to attach a cap to, so it's one
// cap for the whole org. Has NO relationship to budget-increase requests
// (those have no cap check at all in flat mode, admin judgment only) — this
// cap only gates SDLC run spend and managed cloud-endpoint spend.
function OrgWideCapPanel() {
  const [status,   setStatus]   = useState(null); // {cap_usd, consumed_usd, remaining_usd, period_yyyymm, enforcement, ...}
  const [loading,  setLoading]  = useState(true);
  const [error,    setError]    = useState("");
  const [value,    setValue]    = useState("");
  const [saving,   setSaving]   = useState(false);
  const [saveMsg,  setSaveMsg]  = useState("");

  async function load() {
    setLoading(true); setError("");
    try {
      const r = await authFetch(`${API}/budget/admin/org-cap`);
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        setError(d?.detail || `Failed to load org-wide cap (HTTP ${r.status})`);
        return;
      }
      const d = await r.json();
      setStatus(d);
      setValue(String(d.monthly_cap_usd ?? d.cap_usd ?? 0));
    } catch {
      setError("Failed to load org-wide cap.");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { load(); }, []);

  async function save() {
    const n = Number(value);
    if (Number.isNaN(n) || n < 0) { setError("Cap must be a non-negative number."); return; }
    setSaving(true); setError(""); setSaveMsg("");
    try {
      const r = await authFetch(`${API}/budget/admin/org-cap`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ monthly_cap_usd: n }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        const detail = d?.detail;
        setError(Array.isArray(detail) ? detail.map(e => e.msg || JSON.stringify(e)).join("; ") : detail || `Failed (HTTP ${r.status})`);
        return;
      }
      setSaveMsg("Saved.");
      await load();
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-gray-400 py-6">
        <Clock size={13} className="animate-pulse" /> Loading org-wide cap…
      </div>
    );
  }

  const cap       = Number(status?.cap_usd ?? status?.monthly_cap_usd ?? 0);
  const consumed  = Number(status?.consumed_usd ?? 0);
  const remaining = Number(status?.remaining_usd ?? Math.max(0, cap - consumed));
  const pct       = cap > 0 ? Math.min(100, Math.round((consumed / cap) * 100)) : 0;
  const color     = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-yellow-400" : "bg-green-500";

  return (
    <div>
      <h2 className="text-sm font-semibold text-gray-700 mb-1">Org-Wide Monthly Cap</h2>
      <p className="text-xs text-gray-500 mb-4">
        SDLC-run and managed cloud-endpoint spend draw against a single, org-wide
        monthly ceiling. Budget-increase requests are not affected by this cap —
        those are approved purely on admin judgment.
      </p>

      {status && (
        <div className="rounded-lg border border-indigo-100 bg-indigo-50 px-4 py-3 mb-4 text-xs space-y-1 max-w-sm">
          <div className="font-medium text-indigo-700 mb-1 flex items-center justify-between">
            <span>Current period — {status.period_yyyymm}</span>
            {status.enforcement === false && (
              <span className="text-[10px] text-yellow-700 bg-yellow-100 border border-yellow-200 px-1.5 py-0.5 rounded normal-case font-medium">
                Shadow mode
              </span>
            )}
          </div>
          <div className="flex justify-between text-gray-600">
            <span>Consumed</span>
            <span className="font-medium text-orange-600">${consumed.toFixed(2)}</span>
          </div>
          <div className="flex justify-between text-gray-700 font-semibold border-t border-indigo-200 pt-1 mt-1">
            <span>Remaining</span>
            <span className={remaining <= 0 ? "text-red-600" : "text-green-700"}>${remaining.toFixed(2)}</span>
          </div>
          <div className="h-1.5 bg-indigo-200 rounded-full mt-1">
            <div className={`h-1.5 rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}

      <div className="max-w-sm">
        <label className="text-xs text-gray-500">Monthly cap (USD)</label>
        <div className="flex gap-2 mt-1">
          <input
            type="number" step="1" min="0" value={value}
            onChange={e => { setValue(e.target.value); setSaveMsg(""); }}
            className="flex-1 px-3 py-2 border border-gray-200 rounded text-sm outline-none focus:border-indigo-300"
          />
          <button
            onClick={save}
            disabled={saving}
            className="px-4 py-2 bg-blue-600 text-white rounded text-sm brand-grad hover:opacity-70 cursor-pointer disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
        {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
        {saveMsg && <p className="mt-2 text-xs text-green-600">{saveMsg}</p>}
      </div>
    </div>
  );
}

function HodCapsPanel() {
  const [hods,      setHods]      = useState([]);
  const [loading,   setLoading]   = useState(true);
  const [error,     setError]     = useState("");
  const [search,    setSearch]    = useState("");
  const [editing,   setEditing]   = useState(null);
  const [expanded,  setExpanded]  = useState(null);
  const [audit,     setAudit]     = useState({});

  async function toggleAudit(h) {
    const email = h.hod_email;
    if (expanded === email) { setExpanded(null); return; }
    setExpanded(email);
    if (audit[email]) return;
    setAudit(prev => ({ ...prev, [email]: { loading: true, entries: [], rollup: {} } }));
    try {
      const qs = new URLSearchParams({ hod_email: email, period: h.period_yyyymm || "" });
      const r = await authFetch(`${API}/budget/admin/hod-audit?${qs}`);
      const d = r.ok ? await r.json() : { entries: [], rollup: {} };
      const piiOn = await piiEnabled();
      const entries = await Promise.all((d.entries || []).map(async en => ({
        ...en,
        hod_email:   await decryptPii(en.hod_email,   piiOn),
        target_email: await decryptPii(en.target_email, piiOn),
        target_name:  await decryptPii(en.target_name,  piiOn),
      })));
      setAudit(prev => ({ ...prev, [email]: { loading: false, entries, rollup: d.rollup || {} } }));
    } catch {
      setAudit(prev => ({ ...prev, [email]: { loading: false, entries: [], rollup: {} } }));
    }
  }

  async function load() {
    setLoading(true); setError("");
    try {
      const r = await authFetch(`${API}/budget/admin/hods`);
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        setError(d?.detail || `Failed to load HODs (HTTP ${r.status})`);
        setHods([]); return;
      }
      const d = await r.json();
      // hod_email is decrypted here (not just for display) because it is
      // also used verbatim as a URL path segment for the PUT .../cap call
      // below — ciphertext there would 404 against the plaintext DB row.
      const piiOn = await piiEnabled();
      const decrypted = await Promise.all((d.hods || []).map(async h => ({
        ...h,
        hod_email:  await decryptPii(h.hod_email,  piiOn),
        hod_name:   await decryptPii(h.hod_name,   piiOn),
        updated_by: await decryptPii(h.updated_by, piiOn),
      })));
      setHods(decrypted);
    } catch (e) {
      setError(String(e?.message || e));
      setHods([]);
    } finally { setLoading(false); }
  }

  useEffect(() => { load(); }, []);

  const filtered = hods.filter(h => {
    if (!search) return true;
    const q = search.toLowerCase();
    return (h.hod_email || "").toLowerCase().includes(q)
        || (h.hod_name  || "").toLowerCase().includes(q)
        || (h.departments || []).some(d => (d || "").toLowerCase().includes(q));
  });

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-40 text-gray-400">
        <Clock size={28} className="mb-2 animate-pulse" />
        <p className="text-sm">Loading HODs…</p>
      </div>
    );
  }
  if (error) {
    return (
      <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">{error}</div>
    );
  }
  if (!hods.length) {
    return (
      <div className="flex flex-col items-center justify-center h-40 text-gray-300">
        <Users size={32} className="mb-2" />
        <p className="text-sm">No HODs found in department mapping.</p>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-700">HOD Monthly Caps</h2>
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search HOD / department…"
          className="w-64 px-2.5 py-1.5 text-xs border border-gray-200 rounded-md outline-none focus:border-indigo-300 shadow-sm bg-white"
        />
      </div>

      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-4 py-2 text-left text-xs text-gray-500">HOD</th>
              <th className="px-4 py-2 text-left text-xs text-gray-500">Departments</th>
              <th className="px-4 py-2 text-right text-xs text-gray-500">Users</th>
              <th className="px-4 py-2 text-right text-xs text-gray-500">Cap</th>
              <th className="px-4 py-2 text-left text-xs text-gray-500">Consumed (this period)</th>
              <th className="px-4 py-2 text-right text-xs text-gray-500">Remaining</th>
              <th className="px-4 py-2 text-left text-xs text-gray-500">Resets</th>
              <th className="px-4 py-2 text-right text-xs text-gray-500">Action</th>
              <th className="px-4 py-2 text-right text-xs text-gray-500">Audit</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(h => {
              const consumed = +(h.consumed_usd || 0);
              const cap      = h.has_cap_row ? +(h.monthly_cap_usd || 0) : 0;
              const pct      = cap > 0 ? Math.min(100, Math.round((consumed / cap) * 100)) : 0;
              const color    = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-yellow-400" : "bg-green-500";
              const isOpen   = expanded === h.hod_email;
              const auditRow = audit[h.hod_email];
              return (
                <Fragment key={h.hod_email}>
                <tr className="border-t border-gray-100 align-top">
                  <td className="px-4 py-3">
                    <div className="text-gray-800 font-medium truncate max-w-[18ch]">
                      {h.hod_name || h.hod_email}
                    </div>
                    <div className="text-xs text-gray-400 truncate max-w-[24ch]">{h.hod_email}</div>
                    {!h.enforcement && (
                      <span className="inline-block mt-1 text-[10px] text-yellow-700 bg-yellow-100 border border-yellow-200 px-1.5 py-0.5 rounded font-medium">
                        Shadow mode
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-600">
                    <div className="flex flex-wrap gap-1">
                      {(h.departments || []).map(d => (
                        <span key={d} className="text-[11px] bg-gray-100 text-gray-700 border border-gray-200 px-1.5 py-0.5 rounded">{d}</span>
                      ))}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <span className="inline-flex items-center gap-1 text-gray-700 font-medium">
                      <Users size={12} className="text-gray-400" />
                      {h.total_users ?? 0}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    {h.has_cap_row ? (
                      <span className="text-gray-800 font-medium">${cap.toFixed(2)}</span>
                    ) : (
                      <span className="italic text-xs text-gray-400">Not configured</span>
                    )}
                  </td>
                  <td className="px-4 py-3 min-w-[14rem]">
                    {h.has_cap_row ? (
                      <div>
                        <div className="text-xs text-gray-600 mb-1">${consumed.toFixed(2)} / ${cap.toFixed(2)} ({pct}%)</div>
                        <div className="h-1.5 bg-gray-200 rounded-full">
                          <div className={`h-1.5 rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
                        </div>
                      </div>
                    ) : (
                      <div className="flex items-center gap-1 text-gray-500 text-xs">
                        <span>${consumed.toFixed(2)}</span>
                        {consumed > 0 && <AlertTriangle size={12} className="text-yellow-500" />}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right text-gray-700">
                    {h.has_cap_row ? `$${(+(h.remaining_usd || 0)).toFixed(2)}` : "—"}
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">{h.resets_on}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => setEditing(h)}
                      className="cursor-pointer inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-indigo-700 border border-indigo-200 hover:bg-indigo-50"
                    >
                      {h.has_cap_row ? "Edit" : "Add Cap"}
                    </button>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => toggleAudit(h)}
                      className="cursor-pointer inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-gray-600 border border-gray-200 hover:bg-gray-50"
                    >
                      {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      Allocations
                    </button>
                  </td>
                </tr>
                {isOpen && (
                  <tr className="bg-gray-50/60">
                    <td colSpan={9} className="px-4 py-3">
                      <HodAuditTable loading={auditRow?.loading} entries={auditRow?.entries} rollup={auditRow?.rollup} />
                    </td>
                  </tr>
                )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      {editing && (
        <HodCapModal hod={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />
      )}
    </div>
  );
}

// ── HOD Cap Add/Edit modal (kept as-is) ────────────────────────────────────
function HodCapModal({ hod, onClose, onSaved }) {
  const isEdit = !!hod?.has_cap_row;
  const [cap,        setCap]        = useState(isEdit ? (+(hod.monthly_cap_usd || 0)).toFixed(2) : "");
  const [notes,      setNotes]      = useState("");
  const [loading,    setLoading]    = useState(false);
  const [error,      setError]      = useState("");
  const [formErrors, setFormErrors] = useState({ cap: "", notes: "" });
  const [doneFlash,  setDoneFlash]  = useState("");
  const { confirm } = useConfirm();

  function validate() {
    const errs = { cap: "", notes: "" };
    const n = Number(cap);
    if (cap === "" || cap === null || Number.isNaN(n)) errs.cap = "Cap is required";
    else if (n <= 0) errs.cap = "Cap must be greater than 0";
    if (notes) {
      const r = validateDescription(notes);
      if (!r.isValid) errs.notes = r.errors[0]?.message || "Invalid notes";
      if (notes.length > 1000) errs.notes = "Notes must be ≤ 1000 characters";
    }
    return errs;
  }

  async function submit() {
    const errs = validate();
    if (errs.cap || errs.notes) { setFormErrors(errs); return; }
    setLoading(true); setError("");
    try {
      const r = await authFetch(
        `${API}/budget/admin/hods/${encodeURIComponent(hod.hod_email)}/cap`,
        { method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ monthly_cap_usd: Number(cap), notes: notes || null }) },
      );
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        const detail = d?.detail;
        await confirm({
          title: r.status === 404 ? "HOD not found" : "Save failed",
          message: Array.isArray(detail)
                     ? detail.map(e => e.msg || e.message || JSON.stringify(e)).join("; ")
                     : (detail || `HTTP ${r.status}`),
          confirmLabel: "OK", variant: "danger",
        });
        return;
      }
      const d = await r.json();
      setDoneFlash(d?.created ? "Cap created" : "Cap updated");
      setTimeout(() => onSaved && onSaved(), 600);
    } catch (e) {
      setError(String(e?.message || e));
    } finally { setLoading(false); }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md">
        {doneFlash ? (
          <div className="text-center py-4">
            <CheckCircle2 size={40} className="text-green-500 mx-auto mb-3" />
            <h2 className="font-semibold text-gray-800 mb-1">{doneFlash}</h2>
            <p className="text-sm text-gray-500">{hod.hod_email}</p>
          </div>
        ) : (
          <>
            <h2 className="font-semibold text-gray-800 mb-1">
              {isEdit ? "Edit monthly cap" : "Set monthly cap"}
            </h2>
            <p className="text-xs text-gray-500 mb-4 truncate">{hod.hod_email}</p>
            <div className="space-y-3">
              <div>
                <label className="text-xs text-gray-500">Monthly cap (USD) *</label>
                <input
                  type="number" step="0.5" min="0.01" value={cap}
                  onChange={e => { setCap(e.target.value); if (formErrors.cap) setFormErrors(p => ({ ...p, cap: "" })); }}
                  onBlur={() => setFormErrors(p => ({ ...p, cap: validate().cap }))}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none focus:border-indigo-300 ${formErrors.cap ? "border-red-500" : "border-gray-200"}`}
                  placeholder="e.g. 500.00"
                />
                {formErrors.cap && <p className="mt-1 text-xs text-red-600">{formErrors.cap}</p>}
              </div>
              <div>
                <label className="text-xs text-gray-500">Notes (optional)</label>
                <textarea
                  value={notes}
                  onChange={e => { setNotes(e.target.value); if (formErrors.notes) setFormErrors(p => ({ ...p, notes: "" })); }}
                  onBlur={() => setFormErrors(p => ({ ...p, notes: validate().notes }))}
                  rows={3}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none resize-none ${formErrors.notes ? "border-red-500" : "border-gray-200"}`}
                  placeholder="Context for this cap change (optional)"
                />
                {formErrors.notes && <p className="mt-1 text-xs text-red-600">{formErrors.notes}</p>}
              </div>
              {error && <p className="text-xs text-red-600">{error}</p>}
              <div className="flex gap-2 pt-1">
                <button onClick={submit} disabled={loading}
                        className="px-4 py-2 rounded text-sm text-white brand-grad hover:opacity-70 cursor-pointer disabled:opacity-50">
                  {loading ? "Saving…" : (isEdit ? "Save" : "Add Cap")}
                </button>
                <button onClick={onClose} className="px-4 py-2 cursor-pointer border border-gray-200 rounded text-sm hover:bg-gray-100 text-gray-800">
                  Cancel
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Shared budget detail layout (StatCards + increase history + MTD history) ─
// Used by both the My Budget view and the admin/HOD drill-down (UserBudgetDetail)
// so the two views never drift apart. Increase history comes before MTD history,
// with mt-8 spacing between sections.
function BudgetDetailLayout({ base, extra, total, spent, costPct, history, increaseSection, pendingRequestCard, headerLabel, onExtraClick, onUtilClick, increaseOpen = false, showMtd = true, mtdCollapsible = false, extraDrillEnabled = true }) {
  const [mtdOpen, setMtdOpen] = useState(false);
  return (
    <div className="space-y-4">
      {headerLabel && (
        <div className="text-xs font-medium text-gray-500 uppercase tracking-wide">
          {headerLabel}
        </div>
      )}

      {/* StatCards */}
      <div className="flex flex-wrap gap-3">
        <StatCard
          label="Allocated (base)"
          value={`$${base.toFixed(2)}`}
          sub="Standard base — resets monthly"
          tone="neutral"
        />
        <StatCard
          label="Extra budget"
          value={`$${extra.toFixed(2)}`}
          sub={extra > 0 ? "Approved on top of base" : "None yet"}
          tone={extra > 0 ? "green" : "neutral"}
          onClick={extraDrillEnabled ? onExtraClick : undefined}
        />
        <StatCard
          label="Utilisation"
          value={`${costPct}%`}
          sub={`$${spent.toFixed(4)} / $${total.toFixed(2)}`}
          tone={costPct >= 90 ? "red" : costPct >= 70 ? "yellow" : "green"}
          onClick={onUtilClick}
        >
          <div className="mt-2 h-1.5 bg-gray-200 rounded-full">
            <div
              className={`h-1.5 rounded-full transition-all ${
                costPct >= 90 ? "bg-red-500" : costPct >= 70 ? "bg-yellow-400" : "bg-green-500"
              }`}
              style={{ width: `${costPct}%` }}
            />
          </div>
        </StatCard>
      </div>

      {/* Own pending budget-increase request — shown directly below the
          stat cards, disappears automatically once approved or rejected. */}
      {pendingRequestCard}

      {/* Increase history — only when toggled open via the Extra box */}
      {increaseOpen && increaseSection}

      {/* Month-to-Date History (optional / collapsible) */}
      {showMtd && (
        <div className="mt-8">
          {mtdCollapsible ? (
            <button
              onClick={() => setMtdOpen(v => !v)}
              className="flex items-center gap-1.5 text-xs font-medium text-gray-500 uppercase tracking-wide cursor-pointer hover:text-indigo-700"
            >
              {mtdOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              <History size={12} className="text-gray-400" />
              Month-to-Date History
            </button>
          ) : (
            <h3 className="text-xs font-medium text-gray-500 mb-2 uppercase tracking-wide">
              Month-to-Date History
            </h3>
          )}
          {(!mtdCollapsible || mtdOpen) && (
            <div className="mt-2 bg-white border border-gray-200 rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-2 text-left text-xs text-gray-500">Date</th>
                    <th className="px-4 py-2 text-right text-xs text-gray-500">Cost (USD)</th>
                  </tr>
                </thead>
                <tbody>
                  {history.length === 0 ? (
                    <tr>
                      <td colSpan={2} className="px-4 py-4 text-center text-xs text-gray-400 italic">
                        No usage recorded this period.
                      </td>
                    </tr>
                  ) : history.map(row => (
                    <tr key={row.date} className="border-t border-gray-100">
                      <td className="px-4 py-2 text-gray-700">{row.date}</td>
                      <td className="px-4 py-2 text-right text-gray-600">${(row.cost_usd_spent || 0).toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── User budget detail (admin/HOD drill-down) ──────────────────────────────
// Fetches a target user's budget/usage and renders it via the shared
// BudgetDetailLayout. Exported so TeamBudgetPanel can reuse it.
export function UserBudgetDetail({ userId, userName }) {
  const [data,    setData]    = useState(null);
  const [loading, setLoading] = useState(true);
  const [error,  setError]   = useState("");
  // Drill-down sub-view: null | "extra" | "utilization"
  const [drill,   setDrill]   = useState(null);
  // Same platform-wide HOD-hierarchy flag as My Budget — the "Extra budget"
  // card's history drill-down relies on hod_allocation_ledger data that
  // isn't reliably maintained in flat/admin-only mode, so it's disabled
  // there too for every user viewed via this admin/HOD drill-down.
  const [hodMode, setHodMode] = useState(true);
  useEffect(() => { hodModeEnabled().then(setHodMode); }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true); setError("");
      try {
        const r = await authFetch(`${API}/budget/users/${encodeURIComponent(userId)}/usage`);
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          setError(d?.detail || `HTTP ${r.status}`);
          setData(null);
        } else {
          const d = await r.json();
          if (!cancelled) setData(d);
        }
      } catch (e) {
        if (!cancelled) setError(e?.message || String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [userId]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-gray-400 py-6">
        <Clock size={13} className="animate-pulse" /> Loading budget details…
      </div>
    );
  }
  if (error) {
    return (
      <div className="text-xs text-red-600 py-3 px-1">
        Failed to load budget details: {error}
      </div>
    );
  }
  if (!data) return null;

  const b           = data.budget;
  const base        = Number(b?.base_cost_usd  ?? 50);
  const extra       = Number(b?.extra_cost_usd ?? 0);
  const total       = Number(b?.max_cost_usd_total ?? base + extra) || (base + extra);
  const spent       = Number(data.usage_total?.cost_usd_spent ?? 0);
  const costPct     = total > 0 ? Math.min(100, Math.round((spent / total) * 100)) : 0;
  const history     = data.history || [];

  return (
    <div className="space-y-4">
      <BudgetDetailLayout
        base={base}
        extra={extra}
        total={total}
        spent={spent}
        costPct={costPct}
        history={history}
        headerLabel={userName ? `Budget details — ${userName}` : "Budget details"}
        onExtraClick={() => setDrill(drill === "extra" ? null : "extra")}
        increaseOpen={drill === "extra"}
        increaseSection={<UserIncreasesSection userId={userId} />}
        onUtilClick={() => setDrill(drill === "utilization" ? null : "utilization")}
        showMtd={false}
        extraDrillEnabled={hodMode}
      />

      {/* Individual utilization breakdown — shown inline below the stat cards */}
      {drill === "utilization" && (
        <UtilizationPage
          onBack={() => setDrill(null)}
          endpoint={utilizationEndpoints.user(userId)}
          options={[
            { value: "date",    label: "Date wise usage" },
            { value: "channel", label: "Channel wise usage" },
            { value: "model",   label: "Model wise usage" },
          ]}
          defaultView="date"
          history={history}
          showBack={false}
        />
      )}
    </div>
  );
}

// ── Target user's approved-increase audit trail (admin/HOD drill-down) ──────
// Fetches from /budget/admin/hod-audit and filters to the selected user,
// showing the same columns as MyIncreasesSection (when, approved by, amount,
// justification).
function UserIncreasesSection({ userId }) {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    (async () => {
      try {
        const r = await authFetch(`${API}/budget/admin/hod-audit`);
        const d = r.ok ? await r.json() : { entries: [] };
        const filtered = (d.entries || []).filter(
          e => String(e.target_user_id) === String(userId),
        );
        const piiOn = await piiEnabled();
        const decrypted = await Promise.all(filtered.map(async en => ({
          ...en,
          hod_email: await decryptPii(en.hod_email, piiOn),
        })));
        setEntries(decrypted);
      } catch { /* ignore */ }
      setLoading(false);
    })();
  }, [userId]);

  if (loading) return null;
  return (
    <div>
      <h3 className="text-xs font-medium text-gray-500 mb-2 uppercase tracking-wide flex items-center gap-1.5">
        <History size={12} /> Budget increase history
      </h3>
      {!entries.length ? (
        <div className="bg-white border border-gray-200 rounded-lg px-4 py-3 text-xs text-gray-400 italic">
          No approved budget increases for this user.
        </div>
      ) : (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left text-gray-500">When</th>
                <th className="px-3 py-2 text-left text-gray-500">Approved by</th>
                <th className="px-3 py-2 text-right text-gray-500">Amount added</th>
                <th className="px-3 py-2 text-left text-gray-500">Justification</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(e => (
                <tr key={e.id} className="border-t border-gray-100">
                  <td className="px-3 py-2 text-gray-500 whitespace-nowrap">
                    {e.created_at ? toISTDate(new Date(e.created_at)) : "—"}
                  </td>
                  <td className="px-3 py-2 text-gray-700 truncate max-w-[20ch]" title={e.hod_email}>
                    {e.hod_email}
                  </td>
                  <td className="px-3 py-2 text-right font-medium text-green-700 whitespace-nowrap">
                    +${(e.amount_usd || 0).toFixed(2)}
                  </td>
                  <td className="px-3 py-2 text-gray-600 max-w-[30ch]">
                    {e.justification
                      ? <span title={e.justification} className="line-clamp-2">{e.justification}</span>
                      : <span className="text-gray-300">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Read-only user roster (§7) — no allocate/edit/delete. ──────────────────
// Used by both admin and HOD (HOD gets a department-scoped list from the
// backend via /budget/users). Selecting a row expands a full budget detail
// view (UserBudgetDetail) showing utilisation, history, and increases —
// the same information the user sees on their own My Budget page.
const PAGE_SIZE = 50;

function UserRosterPanel({ users, isAdmin, loading = false, error = null, onReload, baseCost = 50, hodMode = true }) {
  const [search,       setSearch]       = useState("");
  const [selected,     setSelected]     = useState(null);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [sortDir,      setSortDir]      = useState(SORT_DESC);   // highest utilisation first

  // Filter by search, then order by utilisation (absolute cost spent).
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = !q ? users : users.filter(u =>
      (u.user_id || "").toLowerCase().includes(q) ||
      (u.email   || "").toLowerCase().includes(q) ||
      (u.name    || "").toLowerCase().includes(q)
    );
    return sortByUtilisation(matched, sortDir);
  }, [users, search, sortDir]);

  function onSearchChange(v) {
    setSearch(v);
    setVisibleCount(PAGE_SIZE);
  }

  function toggleSort() {
    setSortDir(d => toggleSortDirection(d));
    setVisibleCount(PAGE_SIZE);
  }

  const visible = filtered.slice(0, visibleCount);

  return (
    <div>
      <div className="mb-3 flex items-start gap-3 p-3 border border-gray-200 bg-gray-50 rounded-lg">
        <Users size={16} className="text-gray-500 flex-shrink-0 mt-0.5" />
        <div className="text-xs text-gray-700">
          Read-only roster. {isAdmin
            ? `Base allocations are fixed at the platform default; extra budget only changes via ${hodMode ? "approved HOD" : "admin approved"} increase requests.`
            : `You can grant extra budget only by approving increase requests from your team. Base allocation ($${baseCost.toLocaleString()} default) is fixed by the platform.`}
        </div>
      </div>
      <div className="mb-3 flex items-center gap-3">
        <input
          value={search}
          onChange={e => onSearchChange(e.target.value)}
          placeholder="Search users…"
          className="w-full max-w-xs px-3 py-1.5 text-sm border border-gray-200 rounded-md outline-none focus:border-indigo-300 shadow-sm bg-white"
        />
        {loading && (
          <span className="text-xs text-gray-500 italic">Loading roster…</span>
        )}
        {!loading && onReload && (
          <button
            onClick={onReload}
            className="text-xs text-indigo-600 hover:text-indigo-800 underline cursor-pointer"
          >
            Refresh
          </button>
        )}
      </div>
      {error && (
        <div className="mb-3 p-3 border border-red-200 bg-red-50 rounded-lg text-xs text-red-700 flex items-start justify-between gap-3">
          <div>
            <div className="font-medium">Failed to load user roster.</div>
            <div className="text-red-600 mt-0.5 break-words">{error}</div>
          </div>
          {onReload && (
            <button
              onClick={onReload}
              className="flex-shrink-0 px-2 py-1 border border-red-300 rounded text-red-700 hover:bg-red-100 cursor-pointer"
            >
              Retry
            </button>
          )}
        </div>
      )}
      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-4 py-2 text-left text-xs text-gray-500">User</th>
              <th className="px-4 py-2 text-right text-xs text-gray-500">Base</th>
              <th className="px-4 py-2 text-right text-xs text-gray-500">Extra</th>
              <th className="px-4 py-2 text-right text-xs text-gray-500">Total</th>
              <th className="px-4 py-2 text-left text-xs text-gray-500">
                <button
                  type="button"
                  onClick={toggleSort}
                  title={sortDir === SORT_DESC
                    ? "Sorted by highest spend first — click for lowest first"
                    : "Sorted by lowest spend first — click for highest first"}
                  className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-indigo-600 cursor-pointer"
                >
                  Utilisation <ArrowUpDown size={12} />
                </button>
              </th>
              <th className="px-4 py-2 text-right text-xs text-gray-500">Drill-down</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-4 py-6 text-center text-xs text-gray-400 italic">
                  {loading
                    ? "Loading users…"
                    : error
                      ? "Roster unavailable — see error above."
                      : "No users visible."}
                </td>
              </tr>
            ) : visible.map(u => {
              const base  = Number(u.base_cost_usd  ?? 50);
              const extra = Number(u.extra_cost_usd ?? 0);
              const total = Number(u.max_cost_usd_total ?? base + extra) || (base + extra);
              const spent = Number(u.usage_total?.cost_usd_spent ?? 0);
              const pct   = total > 0 ? Math.min(100, Math.round((spent / total) * 100)) : 0;
              const color = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-yellow-400" : "bg-green-500";
              const isOpen = selected?.user_id === u.user_id;
              return (
                <Fragment key={u.user_id}>
                <tr className={`border-t border-gray-100 align-top ${isOpen ? "bg-indigo-50/40" : ""}`}>
                  <td className="px-4 py-3">
                    <div className="text-gray-800 font-medium truncate max-w-[22ch]">{u.name || u.email || u.user_id}</div>
                    <div className="text-xs text-gray-400 truncate max-w-[24ch]">{u.email}</div>
                  </td>
                  <td className="px-4 py-3 text-right text-gray-700">${base.toFixed(2)}</td>
                  <td className="px-4 py-3 text-right text-gray-700">${extra.toFixed(2)}</td>
                  <td className="px-4 py-3 text-right text-gray-800 font-medium">${total.toFixed(2)}</td>
                  <td className="px-4 py-3 min-w-[12rem]">
                    <div className="text-xs text-gray-600 mb-1">${spent.toFixed(2)} / ${total.toFixed(2)} ({pct}%)</div>
                    <div className="h-1.5 bg-gray-200 rounded-full">
                      <div className={`h-1.5 rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
                    </div>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => setSelected(isOpen ? null : u)}
                      className="cursor-pointer inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-gray-600 border border-gray-200 hover:bg-gray-50"
                    >
                      {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      Details
                    </button>
                  </td>
                </tr>
                {isOpen && (
                  <tr className="bg-gray-50/60">
                    <td colSpan={6} className="px-4 py-3">
                      <UserBudgetDetail userId={u.user_id} userName={u.name || u.email} />
                    </td>
                  </tr>
                )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
        {filtered.length > visibleCount && (
          <div className="px-4 py-3 border-t border-gray-100 text-center">
            <button
              onClick={() => setVisibleCount(c => c + PAGE_SIZE)}
              className="px-4 py-2 text-sm text-indigo-600 border border-indigo-200 rounded-md hover:bg-indigo-50 cursor-pointer"
            >
              Load more ({filtered.length - visibleCount} remaining)
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main ────────────────────────────────────────────────────────────────────

export default function BudgetManager({ user }) {
  const IS_ADMIN          = user?.role === "admin";
  const IS_HOD            = !!user?.is_hod;
  const IS_REPORTING_MGR  = !!user?.is_reporting_manager;
  const MY_USER = user?.userId || "";
  // Platform-wide HOD-hierarchy mode (core/config.py::HOD_APPROVAL_ENABLED),
  // NOT a per-user flag — optimistic default of true avoids a flash of
  // flat-mode UI while /auth/ui-config is still loading (matches the byte-
  // for-byte-unchanged HOD-mode behaviour most existing deployments run).
  const [hodMode,       setHodMode]       = useState(true);
  useEffect(() => { hodModeEnabled().then(setHodMode); }, []);
  // Platform base budget allocation (default $50, configurable via
  // BUDGET_BASE_COST_USD) — optimistic default of 50 while /auth/ui-config
  // is still loading.
  const [baseCost,      setBaseCost]      = useState(50);
  useEffect(() => { baseCostUsd().then(setBaseCost); }, []);
  const [adminView,     setAdminView]     = useState(false);
  const [teamView,      setTeamView]      = useState(false);
  const [users,         setUsers]         = useState([]);
  const [myBudget,      setMyBudget]      = useState(null);
  const [hodCap,        setHodCap]        = useState(null);
  const [showRequest,   setShowRequest]   = useState(false);
  const [adminTab,      setAdminTab]      = useState("users");  // "users" | "requests" | "hod_caps"
  const [teamTab,       setTeamTab]       = useState("users");  // "users" | "requests"
  const [myAudit,       setMyAudit]       = useState(null);
  const [showMyAudit,   setShowMyAudit]   = useState(false);
  const [usersLoading,  setUsersLoading]  = useState(false);
  const [usersError,    setUsersError]    = useState(null);
  // My Budget drill-down sub-view: null | "extra" | "utilization"
  const [myDrill,       setMyDrill]       = useState(null);
  // Bumped after submitting a new request so MyPendingRequestCard re-fetches
  // immediately instead of waiting for its own poll/mount cycle.
  const [myPendingRefresh, setMyPendingRefresh] = useState(0);
  // Mirrors MyPendingRequestCard's own fetch — true while the caller has an
  // unresolved budget-increase request, so the "Request Increase" button can
  // be disabled (with a tooltip) instead of letting them file a duplicate
  // that the backend would reject anyway.
  const [hasPendingRequest, setHasPendingRequest] = useState(false);
  // Whether the caller has been nominated as a delegatee by any HOD — drives
  // visibility of the Team button/tab for users who are otherwise neither an
  // HOD nor a reporting manager (a plain individual contributor can still be
  // delegated approval authority). null = not checked yet.
  const [isDelegate,    setIsDelegate]    = useState(null);

  useEffect(() => {
    loadMyBudget();
    if (adminView) loadUsers();
    if (teamView && IS_HOD) { loadHodCap(); loadMyAudit(); }
  }, [adminView, teamView]);

  useEffect(() => {
    // Always checked, independent of IS_HOD/IS_REPORTING_MGR — an HOD or
    // reporting manager can ALSO be delegated approval authority by another
    // HOD, in which case they should see the "Delegated" sub-tab under Team
    // in addition to their own department roster / pending-requests tabs.
    (async () => {
      try {
        const r = await authFetch(`${API}/budget/delegate/cap-status`);
        const d = r.ok ? await r.json() : { delegating_hods: [] };
        setIsDelegate((d.delegating_hods || []).length > 0);
      } catch {
        setIsDelegate(false);
      }
    })();
  }, []);

  async function loadUsers() {
    setUsersLoading(true);
    setUsersError(null);
    const ctrl  = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 45_000);
    try {
      const r = await authFetch(`${API}/budget/users`, { signal: ctrl.signal });
      if (!r.ok) {
        // Try to surface the server's detail for debugging.
        let detail = `HTTP ${r.status}`;
        try {
          const j = await r.json();
          if (j?.detail) detail += ` — ${j.detail}`;
        } catch { /* body wasn't JSON */ }
        throw new Error(detail);
      }
      const d = await r.json();
      const rawUsers = Array.isArray(d?.users) ? d.users : [];
      const piiOn = await piiEnabled();
      const decrypted = await Promise.all(rawUsers.map(async u => ({
        ...u,
        email: await decryptPii(u.email, piiOn),
        name:  await decryptPii(u.name,  piiOn),
      })));
      setUsers(decrypted);
    } catch (e) {
      const msg = e?.name === "AbortError"
        ? "Request timed out after 45s — the roster is still loading on the server. Please retry."
        : (e?.message || String(e));
      // eslint-disable-next-line no-console
      console.error("BudgetManager.loadUsers failed:", e);
      setUsersError(msg);
      setUsers([]);
    } finally {
      clearTimeout(timer);
      setUsersLoading(false);
    }
  }

  async function loadMyBudget() {
    const r = await authFetch(`${API}/budget/me`, { headers: { "X-User-Id": MY_USER } });
    if (r.ok) {
      const d = await r.json();
      const piiOn = await piiEnabled();
      d.hod_email = await decryptPii(d.hod_email, piiOn);
      d.hod_name  = await decryptPii(d.hod_name,  piiOn);
      d.delegatee_emails = await Promise.all((d.delegatee_emails || []).map((e) => decryptPii(e, piiOn)));
      setMyBudget(d);
    }
  }

  async function loadHodCap() {
    try {
      const r = await authFetch(`${API}/budget/hod/cap-status`);
      if (r.ok) {
        const d = await r.json();
        setHodCap(d?.is_hod ? d : null);
      }
    } catch { /* ignore */ }
  }

  async function loadMyAudit() {
    try {
      const r = await authFetch(`${API}/budget/admin/hod-audit`);
      if (r.ok) {
        const d = await r.json();
        const piiOn = await piiEnabled();
        const entries = await Promise.all((d.entries || []).map(async en => ({
          ...en,
          hod_email:    await decryptPii(en.hod_email,    piiOn),
          target_email: await decryptPii(en.target_email, piiOn),
          target_name:  await decryptPii(en.target_name,  piiOn),
        })));
        setMyAudit({ entries, rollup: d.rollup || {} });
      }
    } catch { /* ignore */ }
  }

  function switchToAdmin() { setAdminView(true); setTeamView(false); setMyDrill(null); }
  function switchToTeam()  {
    setAdminView(false); setTeamView(true); setMyDrill(null);
    // A pure delegatee (not an HOD or reporting manager) has no department
    // roster of their own — land them straight on "Delegated" instead of
    // the empty "Department Users" tab.
    if (!IS_HOD && !IS_REPORTING_MGR && isDelegate) setTeamTab("delegated");
  }
  function switchToMine()  { setAdminView(false); setTeamView(false); }

  // ── Derived My-Budget values ─────────────────────────────────────────────
  const b           = myBudget?.budget;
  const base        = Number(b?.base_cost_usd  ?? 50);
  const extra       = Number(b?.extra_cost_usd ?? 0);
  const total       = Number(b?.max_cost_usd_total ?? base + extra) || (base + extra);
  const spent       = Number(myBudget?.usage_total?.cost_usd_spent ?? 0);
  const costPct     = total > 0 ? Math.min(100, Math.round((spent / total) * 100)) : 0;
  const nearLimit = costPct >= 80;
  // Single source of truth for the per-request ceiling: GET /budget/me's
  // `max_request_extra_usd`, which mirrors BUDGET_MAX_INCREASE_PER_REQUEST_USD
  // on the backend (default 200) — no separate hardcoded platform ceiling.
  const maxRequestExtraUsd = Number(myBudget?.max_request_extra_usd) > 0
    ? Number(myBudget.max_request_extra_usd) : DEFAULT_MAX_REQUEST_EXTRA_USD;
  const budgetAtMax = extra >= maxRequestExtraUsd; // gating for the extra request

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="border-b border-gray-200 p-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <DollarSign size={18} className="text-indigo-700" />
          <h1 className="text-sm font-semibold text-indigo-700">Budget Manager</h1>
        </div>
        <div className="flex cursor-pointer gap-2 rounded overflow-hidden text-sm">
          {IS_ADMIN && (
            <button
              onClick={switchToAdmin}
              className={`px-2.5 py-1 cursor-pointer flex items-center gap-1.5 transition rounded ${adminView ? "brand-grad hover:opacity-70 text-white" : "bg-gray-50 text-gray-800 border border-gray-100 hover:bg-gray-100"}`}
            >
              <Users size={13} /> Admin
            </button>
          )}
          {IS_HOD && (
            <button
              onClick={switchToTeam}
              className={`px-2.5 py-1 cursor-pointer flex items-center gap-1.5 transition rounded ${teamView ? "brand-grad hover:opacity-70 text-white" : "bg-gray-50 text-gray-800 border border-gray-100 hover:bg-gray-100"}`}
            >
              <Users size={13} /> Team
            </button>
          )}
          {IS_REPORTING_MGR && (
            <button
              onClick={switchToTeam}
              className={`px-2.5 py-1 cursor-pointer flex items-center gap-1.5 transition rounded ${teamView ? "brand-grad hover:opacity-70 text-white" : "bg-gray-50 text-gray-800 border border-gray-100 hover:bg-gray-100"}`}
            >
              <Users size={13} /> Team
            </button>
          )}
          {!IS_HOD && !IS_REPORTING_MGR && isDelegate && (
            <button
              onClick={switchToTeam}
              className={`px-2.5 py-1 cursor-pointer flex items-center gap-1.5 transition rounded ${teamView ? "brand-grad hover:opacity-70 text-white" : "bg-gray-50 text-gray-800 border border-gray-100 hover:bg-gray-100"}`}
            >
              <Users size={13} /> Team
            </button>
          )}
          <button
            onClick={switchToMine}
            className={`px-2.5 py-1 cursor-pointer flex items-center gap-1.5 transition rounded ${!adminView && !teamView ? "brand-grad hover:opacity-70 text-white" : "bg-gray-50 text-gray-800 border border-gray-100 hover:bg-gray-100"}`}
          >
            <User size={13} /> My Budget
          </button>
        </div>
      </div>

      {/* MY BUDGET VIEW (§7b + §7c) */}
      {!adminView && !teamView && (
        <div className="flex-1 overflow-y-auto p-6 bg-white">
          <div className="max-w-3xl mx-auto px-8 py-6">
            {/* Near-limit warning — hidden only on the utilization sub-page */}
            {myDrill !== "utilization" && nearLimit && (
              <div className="mb-4 p-3 bg-yellow-50 border border-yellow-200 rounded-lg flex items-start gap-2">
                <AlertTriangle size={15} className="text-yellow-600 flex-shrink-0 mt-0.5" />
                <div className="flex-1">
                  <p className="text-sm text-yellow-800 font-medium">
                    Budget {costPct >= 100 ? "exhausted" : "running low"}
                  </p>
                  <p className="text-xs text-yellow-700 mt-0.5">
                    Cost: {costPct}% used (${spent.toFixed(4)} of ${total.toFixed(2)}).
                    {costPct >= 100 && " New requests are being blocked."}
                  </p>
                </div>
              </div>
            )}

            {/* Persistent request-increase action for My Budget — hidden only on the utilization sub-page */}
            {myDrill !== "utilization" && (
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-sm font-semibold text-gray-700">My Budget</h2>
                <button
                  onClick={() => setShowRequest(true)}
                  disabled={budgetAtMax || hasPendingRequest}
                  title={hasPendingRequest ? "Request already pending" : undefined}
                  className={`px-3 py-1.5 text-xs rounded flex items-center gap-1.5 ${
                    budgetAtMax || hasPendingRequest
                      ? "bg-gray-100 text-gray-400 cursor-not-allowed"
                      : "brand-grad text-white hover:opacity-70 cursor-pointer"
                  }`}
                >
                  <AlertTriangle size={12} />
                  {budgetAtMax ? "Max extra reached" : hasPendingRequest ? "Request pending" : "Request Increase"}
                </button>
              </div>
            )}

            {!b ? (
              <div className="text-gray-400 text-sm">No budget configured for your account.</div>
            ) : myDrill === "utilization" ? (
              <UtilizationPage
                onBack={() => setMyDrill(null)}
                endpoint={utilizationEndpoints.me()}
                headers={{ "X-User-Id": MY_USER }}
                options={[
                  { value: "date",    label: "Date wise usage" },
                  { value: "channel", label: "Channel wise usage" },
                  { value: "model",   label: "Model wise usage" },
                ]}
                defaultView="date"
                history={myBudget.history || []}
              />
            ) : (
              <BudgetDetailLayout
                base={base}
                extra={extra}
                total={total}
                spent={spent}
                costPct={costPct}
                history={myBudget.history || []}
                showMtd={false}
                increaseOpen={myDrill === "extra"}
                increaseSection={<MyIncreasesSection userId={MY_USER} />}
                pendingRequestCard={<MyPendingRequestCard refreshKey={myPendingRefresh} onPendingChange={setHasPendingRequest} hodMode={hodMode} />}
                onExtraClick={() => setMyDrill(myDrill === "extra" ? null : "extra")}
                onUtilClick={() => setMyDrill("utilization")}
                // Budget-increase history (the "Extra budget" card's expand-to-history
                // affordance) relies on hod_allocation_ledger data that isn't reliably
                // maintained in flat/admin-only mode (HOD_APPROVAL_ENABLED=false) — so
                // the drill-down is disabled there for every user. HOD-hierarchy mode
                // (true) keeps the existing behaviour unchanged.
                extraDrillEnabled={hodMode}
              />
            )}

            {!b && (
              <button
                onClick={() => setShowRequest(true)}
                className="mt-4 cursor-pointer flex items-center gap-1.5 px-4 py-2 text-white text-sm rounded brand-grad hover:opacity-70"
              >
                Request Budget Allocation
              </button>
            )}
          </div>
        </div>
      )}

      {/* ADMIN / HOD VIEW */}
      {adminView && (
        <div className="flex flex-col flex-1 overflow-hidden">
          {/* Sub-tabs */}
          <div className="flex border-b border-gray-200 px-4">
            <button
              onClick={() => setAdminTab("users")}
              className={`px-4 py-2.5 cursor-pointer text-sm font-medium border-b-2 transition-colors ${adminTab === "users" ? "border-indigo-500 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"}`}
            >
              User Budgets
            </button>
            <button
              onClick={() => setAdminTab("requests")}
              className={`px-4 py-2.5 cursor-pointer text-sm font-medium border-b-2 transition-colors ${adminTab === "requests" ? "border-indigo-500 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"}`}
            >
              {IS_ADMIN ? "Budget Requests" : "Team Pending Requests"}
            </button>
            {IS_ADMIN && hodMode && (
              <button
                onClick={() => setAdminTab("hod_caps")}
                className={`px-4 py-2.5 cursor-pointer text-sm font-medium border-b-2 transition-colors ${adminTab === "hod_caps" ? "border-indigo-500 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"}`}
              >
                HOD Caps
              </button>
            )}
            {IS_ADMIN && !hodMode && (
              <button
                onClick={() => setAdminTab("org_cap")}
                className={`px-4 py-2.5 cursor-pointer text-sm font-medium border-b-2 transition-colors ${adminTab === "org_cap" ? "border-indigo-500 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"}`}
              >
                Org-Wide Cap
              </button>
            )}
          </div>

          {adminTab === "requests" ? (
            <div className="flex-1 overflow-y-auto p-6 max-w-3xl mx-auto w-full">
              <PendingRequests isAdmin={IS_ADMIN} isHod={false} hodMode={hodMode} onChange={() => { loadUsers(); }} />
            </div>
          ) : adminTab === "hod_caps" && hodMode ? (
            <div className="flex-1 overflow-y-auto p-6 max-w-6xl mx-auto w-full">
              <HodCapsPanel />
            </div>
          ) : adminTab === "org_cap" && !hodMode ? (
            <div className="flex-1 overflow-y-auto p-6 max-w-2xl mx-auto w-full">
              <OrgWideCapPanel />
            </div>
          ) : (
            <div className="flex-1 overflow-y-auto p-6 max-w-6xl mx-auto w-full">
              <UserRosterPanel
                users={users}
                isAdmin={IS_ADMIN}
                loading={usersLoading}
                error={usersError}
                onReload={loadUsers}
                baseCost={baseCost}
                hodMode={hodMode}
              />
            </div>
          )}
        </div>
      )}

      {/* TEAM VIEW — reporting manager / HOD read-only */}
      {teamView && (
        <div className="flex flex-col flex-1 overflow-hidden">
          {/* HOD monthly cap banner — only for HODs (IS_HOD is already forced
              false platform-wide when !hodMode via Tier-1 suppression in
              auth/dependencies.py, but hodMode is checked explicitly too as
              defense-in-depth against stale is_hod state). */}
          {IS_HOD && hodMode && hodCap && (() => {
            const cap   = +(hodCap.cap_usd || 0);
            const used  = +(hodCap.consumed_usd || 0);
            const pct   = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
            const color = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-yellow-400" : "bg-green-500";
            return (
              <div className="border-b border-gray-200 px-4 py-3 bg-indigo-50/40">
                <div className="max-w-3xl mx-auto">
                  <div className="flex items-baseline justify-between mb-1">
                    <span className="text-xs font-semibold text-gray-700 uppercase tracking-wide">
                      Your monthly allocation cap
                      {!hodCap.has_cap_row && <span className="ml-2 text-[10px] text-gray-400 normal-case font-normal">(default)</span>}
                      {!hodCap.enforcement && (
                        <span className="ml-2 text-[10px] text-yellow-700 bg-yellow-100 border border-yellow-200 px-1.5 py-0.5 rounded normal-case font-medium">
                          Shadow mode
                        </span>
                      )}
                    </span>
                    <span className="text-xs text-gray-500">Resets on {hodCap.resets_on}</span>
                  </div>
                  <div className="flex justify-between text-xs text-gray-600 mb-1">
                    <span>${used.toFixed(2)} used · ${(+(hodCap.remaining_usd || 0)).toFixed(2)} remaining</span>
                    <span className="font-medium text-gray-700">${used.toFixed(2)} / ${cap.toFixed(2)} ({pct}%)</span>
                  </div>
                  <div className="h-2 bg-gray-200 rounded-full">
                    <div className={`h-2 rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
                  </div>
                </div>
              </div>
            );
          })()}

          {/* HOD's own increases-approved list, collapsible */}
          {IS_HOD && myAudit && (
            <div className="border-b border-gray-200 px-4 py-3 bg-white">
              <div className="max-w-3xl mx-auto">
                <button
                  onClick={() => setShowMyAudit(v => !v)}
                  className="flex items-center gap-1.5 text-xs font-semibold text-gray-700 uppercase tracking-wide cursor-pointer hover:text-indigo-700"
                >
                  {showMyAudit ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                  <History size={13} className="text-gray-400" />
                  Increases you approved this period
                  <span className="ml-1 normal-case font-normal text-gray-400">
                    ({(myAudit.entries || []).length})
                  </span>
                </button>
                {showMyAudit && (
                  <div className="mt-3">
                    <HodAuditTable entries={myAudit.entries} rollup={myAudit.rollup} />
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Team sub-tabs */}
          <div className="flex border-b border-gray-200 px-4">
            {(IS_HOD || IS_REPORTING_MGR) && (
              <button
                onClick={() => setTeamTab("users")}
                className={`px-4 py-2.5 cursor-pointer text-sm font-medium border-b-2 transition-colors ${teamTab === "users" ? "border-indigo-500 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"}`}
              >
                Department Users
              </button>
            )}
            {IS_HOD && (
              <button
                onClick={() => setTeamTab("requests")}
                className={`px-4 py-2.5 cursor-pointer text-sm font-medium border-b-2 transition-colors ${teamTab === "requests" ? "border-indigo-500 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"}`}
              >
                Pending Requests
              </button>
            )}
            {/* Delegation is a HOD-hierarchy-only concept — hidden entirely
                in flat/admin-only mode, on top of IS_HOD already being
                Tier-1-suppressed there. */}
            {IS_HOD && hodMode && (
              <button
                onClick={() => setTeamTab("delegation")}
                className={`px-4 py-2.5 cursor-pointer text-sm font-medium border-b-2 transition-colors ${teamTab === "delegation" ? "border-indigo-500 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"}`}
              >
                Delegation
              </button>
            )}
            {isDelegate && hodMode && (
              <button
                onClick={() => setTeamTab("delegated")}
                className={`px-4 py-2.5 cursor-pointer text-sm font-medium border-b-2 transition-colors ${teamTab === "delegated" ? "border-indigo-500 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"}`}
              >
                Delegated
              </button>
            )}
          </div>

          {teamTab === "requests" && IS_HOD ? (
            <div className="flex-1 overflow-y-auto p-6 max-w-3xl mx-auto w-full">
              <PendingRequests isAdmin={false} isHod={true} scope="hod" hodMode={hodMode} onChange={loadHodCap} />
            </div>
          ) : teamTab === "delegation" && IS_HOD && hodMode ? (
            <div className="flex-1 overflow-y-auto p-6 max-w-3xl mx-auto w-full">
              <DelegationPanel />
            </div>
          ) : teamTab === "delegated" && isDelegate && hodMode ? (
            <div className="flex-1 overflow-y-auto p-6 max-w-3xl mx-auto w-full">
              <TeamDelegatedPanel />
            </div>
          ) : (IS_HOD || IS_REPORTING_MGR) ? (
            <TeamBudgetPanel user={user} />
          ) : hodMode ? (
            <div className="flex-1 overflow-y-auto p-6 max-w-3xl mx-auto w-full">
              <TeamDelegatedPanel />
            </div>
          ) : null}
        </div>
      )}

      {showRequest && (
        // Self-increase shortcut: HODs (HOD_APPROVAL_ENABLED=true) always get
        // it; in flat/admin-only mode (!hodMode) admins get the same
        // immediate-apply shortcut instead of filing a request to themselves.
        (IS_HOD || (IS_ADMIN && !hodMode)) ? (
          <HodSelfIncreaseModal
            currentBudget={myBudget?.budget}
            onClose={() => { setShowRequest(false); loadMyBudget(); }}
            user={user}
            isAdminActor={!IS_HOD}
            maxRequestExtraUsd={maxRequestExtraUsd}
          />
        ) : (
          <RequestIncreaseModal
            currentBudget={myBudget?.budget}
            hodEmail={myBudget?.hod_email}
            hodName={myBudget?.hod_name}
            delegateeEmails={myBudget?.delegatee_emails || []}
            onClose={() => { setShowRequest(false); loadMyBudget(); setMyPendingRefresh(v => v + 1); }}
            user={user}
            budgetAtMax={budgetAtMax}
            hodMode={hodMode}
            maxRequestExtraUsd={maxRequestExtraUsd}
          />
        )
      )}
    </div>
  );
}
