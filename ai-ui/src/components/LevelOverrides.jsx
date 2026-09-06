// SPDX-License-Identifier: MIT
// ============================================================
// LEVEL OVERRIDES SCREEN
// Visible to: ad_level <= 2 (Director+) OR admin
// Allows granting/revoking temporary AD-level promotions.
// Overrides survive nightly org_tree TRUNCATE+INSERT.
// ============================================================
import { useState, useEffect, useRef, useCallback } from "react";
import { API_BASE as API, authFetch, apiFetch } from "../config";
import { toISTDate } from "../utils/time";
import { useToast, useConfirm } from './ui/DialogProvider.jsx';
import { UserCircle } from "lucide-react";
import { validateDescription, validateSecurity } from "../utils/securityValidation";
import { decryptPii } from "../utils/piiCrypto";

// expires_at on this screen is stored VERBATIM — no UTC/IST conversion in
// either direction (see grantOverride / backend create_level_override).
// Format the raw digits directly; do NOT use toIST()/toISTDate() here, those
// add a +5:30 shift that assumes the value is stored as UTC, which this
// field deliberately is not.
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function formatExpiryLiteral(ts) {
  if (!ts) return "—";
  // ts looks like "2026-08-04T15:30:00" (from datetime-local input, or ISO from the DB)
  const m = String(ts).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) return String(ts);
  const [, y, mo, d, h, min] = m;
  const hour12 = ((+h + 11) % 12) + 1;
  const ampm = +h < 12 ? "AM" : "PM";
  return `${d} ${MONTHS[+mo - 1]} ${y}, ${String(hour12).padStart(2, "0")}:${min} ${ampm}`;
}

  // Module-level constant — stable reference, never recreated on render
const LO_EMPTY_ERRORS = { grantReason: "", historyUserId: "", searchEmail: "" };

const LO_EMPTY_FORM_STATE = {
  searchEmail: "",
  searchResults: [],
  selectedUser: null,
  grantLevel: 3,
  grantReason: "",
  grantExpiry: "",
};

const LEVEL_LABELS = {
  0: "L0 – Executive",
  1: "L1 – VP",
  2: "L2 – Director",
  3: "L3 – Senior Manager",
  4: "L4 – Manager",
  5: "L5 – Senior Engineer",
  6: "L6 – Engineer",
};

// Flat/admin-only mode (HOD_APPROVAL_ENABLED=false, the default): no real
// department/seniority hierarchy exists, so overrides collapse to a binary
// grant instead of the full L0–L6 range. Mirrors the backend constraint in
// routers/auth_router.py::create_level_override.
const FLAT_LEVEL_LABELS = {
  0: "Elevated (admin-equivalent access)",
  6: "Standard (default access)",
};

export default function LevelOverrides({ user }) {
  const { confirm } = useConfirm();
  const adLevel = user?.ad_level ?? 6;
  const isAdmin = user?.role === "admin";

  // NOTE: the access guard deliberately lives AFTER every hook below (search
  // for "Access guard"). It used to sit here, above them, which broke this
  // screen outright.
  //
  // `user` arrives as null on first paint and is populated once /auth/me
  // resolves, so `adLevel` starts at the default 6 and the guard returned
  // early — calling ZERO hooks. When the session hydrated with a real
  // Director-level user the guard stopped firing and all 20+ hooks ran for the
  // first time on an already-mounted component. React compares hook counts
  // between renders, so it threw "Rendered more hooks than during the previous
  // render" and the ErrorBoundary in App.jsx swallowed the screen — which is
  // why the user search box appeared inert: its onChange never reached a live
  // component. Hooks must run unconditionally; only the returned JSX may be
  // conditional.
  const [activeTab, setActiveTab]     = useState("active");
  const [overrides, setOverrides]     = useState([]);
  const [loading, setLoading]         = useState(false);
  const [error, setError]             = useState("");
  const [success, setSuccess]         = useState("");

  // PII payload encryption flag (core/pii_crypto.py) — fetched once from the
  // unauthenticated /auth/ui-config endpoint; used to decrypt "pii:v1:"
  // email/name fields returned by /auth/users and /auth/level-overrides*.
  const piiEnabledPromise = useRef(
    apiFetch(`${API}/auth/ui-config`)
      .then(r => r.ok ? r.json() : null)
      .then(d => !!d?.pii_payload_encryption_enabled)
      .catch(() => false)
  );

  // Platform-wide HOD-hierarchy mode (core/config.py::HOD_APPROVAL_ENABLED).
  // False (the default, flat/admin-only): only admins may manage overrides,
  // and the grantable levels collapse to a binary Elevated(L0)/Standard(L6)
  // choice — see routers/auth_router.py::_require_director/create_level_override.
  const [hodMode, setHodMode] = useState(true);
  useEffect(() => {
    apiFetch(`${API}/auth/ui-config`)
      .then(r => r.ok ? r.json() : null)
      .then(d => setHodMode(!!d?.hod_approval_enabled))
      .catch(() => setHodMode(true));
  }, []);

  // Grant form state
  const [searchEmail, setSearchEmail]   = useState("");
  const [searchResults, setSearchResults] = useState([]);
  const [selectedUser, setSelectedUser] = useState(null);
  const [grantLevel, setGrantLevel]     = useState(isAdmin ? 0 : Math.max(0, adLevel));
  // Reset to the flat-mode default (Standard/L6) once /auth/ui-config resolves
  // and hodMode turns out to be false — the initial state above assumes
  // HOD-hierarchy mode since ui-config hasn't loaded yet.
  useEffect(() => {
    if (!hodMode) setGrantLevel(isAdmin ? 0 : 6);
  }, [hodMode]);
  const [grantReason, setGrantReason]   = useState("");
  const [grantExpiry, setGrantExpiry]   = useState("");
  const [submitting, setSubmitting]     = useState(false);
  const searchTimer = useRef(null);

  // History state
  const [historyUserId, setHistoryUserId]       = useState("");   // UUID (internal)
  const [historySearchEmail, setHistorySearchEmail] = useState("");
  const [historySearchResults, setHistorySearchResults] = useState([]);
  const [historySelectedUser, setHistorySelectedUser]   = useState(null);
  const historySearchTimer = useRef(null);
  const [history, setHistory]             = useState([]);

  // Form validation
  const [formErrors, setFormErrors] = useState(LO_EMPTY_ERRORS);

  function validateField(fieldName, value) {
    switch (fieldName) {
      case "grantReason": {
        if (!value || !value.trim()) return "Reason is required";
        const result = validateDescription(value);
        if (!result.isValid && result.errors.length > 0) {
          return result.errors[0]?.message || "Invalid input";
        }
        return "";
      }
      case "historyUserId": {
        if (!value || !value.trim()) return "";
        const result = validateSecurity(value.trim(), { checkSQL: false });
        if (!result.isValid && result.errors.length > 0) {
          return result.errors[0]?.message || "Invalid input";
        }
        return "";
      }
      case "searchEmail": {
        if (!value || !value.trim()) return "";
        // For email search, only check SQL injection and XSS, not special chars (emails need @ and .)
        const result = validateSecurity(value.trim(), { checkSQL: true });
        if (!result.isValid && result.errors.length > 0) {
          // Filter out special_chars errors since @ and . are valid in emails
          const filteredErrors = result.errors.filter(e => e.pattern !== "special_chars");
          if (filteredErrors.length > 0) {
            return filteredErrors[0]?.message || "Invalid input";
          }
        }
        return "";
      }
      default:
        return "";
    }
  }

  function handleBlur(fieldName, value) {
    const err = validateField(fieldName, value);
    setFormErrors(prev => ({ ...prev, [fieldName]: err }));
  }

  function handleChange(fieldName, value, setter) {
    setter(value);
    // Clear the error for this field when user starts typing
    setFormErrors(prev => ({ ...prev, [fieldName]: "" }));
  }

  function resetGrantForm() {
    setSearchEmail("");
    setSearchResults([]);
    setSelectedUser(null);
    setGrantLevel(isAdmin ? 0 : (hodMode ? adLevel : 6));
    setGrantReason("");
    setGrantExpiry("");
    setFormErrors(LO_EMPTY_ERRORS);
    setError("");
    setSuccess("");
  }

  const loadOverrides = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await authFetch(`${API}/auth/level-overrides`);
      const data = await res.json();
      const piiOn = await piiEnabledPromise.current;
      const decrypted = await Promise.all((data.overrides || []).map(async ov => ({
        ...ov,
        user_email:    await decryptPii(ov.user_email,    piiOn),
        user_name:     await decryptPii(ov.user_name,     piiOn),
        grantor_email: await decryptPii(ov.grantor_email, piiOn),
      })));
      setOverrides(decrypted);
    } catch (e) {
      setError("Failed to load overrides");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadOverrides();
  }, [loadOverrides]);

  // ── Access guard ────────────────────────────────────────────────
  // Placed here, after every hook above, so the hook order is identical on
  // every render regardless of whether `user` has loaded yet. Returning early
  // is safe at this point; doing it before the hooks is not (see the note at
  // the top of this component).
  // Flat/admin-only mode (HOD_APPROVAL_ENABLED=false, the default): no real
  // department/seniority hierarchy exists, so only admins may manage
  // overrides — the ad_level<=2 (Director+) path only applies in
  // HOD-hierarchy mode. Mirrors routers/auth_router.py::_require_director.
  if (!isAdmin && (!hodMode || adLevel > 2)) {
    return (
      <div className="p-8 text-gray-500 text-sm">
        {hodMode
          ? "Access restricted. Only Director-level users (L0–L2) or admins can manage level overrides."
          : "Access restricted. Only admins can manage level overrides in this deployment."}
      </div>
    );
  }

  async function searchUsers(email) {
    if (!email || email.length < 2) { setSearchResults([]); return; }
    try {
      const res  = await authFetch(`${API}/auth/users?search=${encodeURIComponent(email)}&page_size=10`);
      const data = await res.json();
      const piiOn = await piiEnabledPromise.current;
      const decrypted = await Promise.all((data.users || []).map(async u => ({
        ...u,
        email: await decryptPii(u.email, piiOn),
        name:  await decryptPii(u.name,  piiOn),
      })));
      setSearchResults(decrypted);
    } catch { setSearchResults([]); }
  }

  function handleSearchChange(value) {
    handleChange("searchEmail", value, setSearchEmail);
    clearTimeout(searchTimer.current);
    if (!value || value.trim().length < 2) {
      setSearchResults([]);
      return;
    }
    searchTimer.current = setTimeout(() => {
      searchUsers(value.trim());
    }, 250);
  }

  function handleHistorySearchChange(value) {
    setHistorySearchEmail(value);
    setHistorySelectedUser(null);
    setHistoryUserId("");
    setHistory([]);
    clearTimeout(historySearchTimer.current);
    if (!value || value.trim().length < 2) {
      setHistorySearchResults([]);
      return;
    }
    historySearchTimer.current = setTimeout(async () => {
      try {
        const res  = await authFetch(`${API}/auth/users?search=${encodeURIComponent(value.trim())}&page_size=10`);
        const data = await res.json();
        const piiOn = await piiEnabledPromise.current;
        const decrypted = await Promise.all((data.users || []).map(async u => ({
          ...u,
          email: await decryptPii(u.email, piiOn),
          name:  await decryptPii(u.name,  piiOn),
        })));
        setHistorySearchResults(decrypted);
      } catch { setHistorySearchResults([]); }
    }, 250);
  }

  async function grantOverride(e) {
    e.preventDefault();
    if (!selectedUser) { setError("Select a user first"); return; }

    // Validate all fields before submission
    const errors = {};
    const reasonErr = validateField("grantReason", grantReason);
    if (reasonErr) errors.grantReason = reasonErr;
    
    const searchErr = validateField("searchEmail", searchEmail);
    if (searchErr) errors.searchEmail = searchErr;

    if (Object.keys(errors).length > 0) {
      setFormErrors(prev => ({ ...prev, ...errors }));
      return;
    }

    setSubmitting(true);
    setError(""); setSuccess("");
    try {
      // Send the datetime-local value exactly as typed (e.g. "2026-08-04T15:30") —
      // no timezone conversion. The backend stores these exact digits verbatim,
      // and the UI displays them verbatim, so what the grantor picks is byte-for-byte
      // what ends up in the DB and on screen. No UTC/IST math anywhere in this field.
      const body = {
        user_id:           selectedUser.id,
        ad_level_override: grantLevel,
        reason:            grantReason,
        expires_at:        grantExpiry || null,
      };
      const res = await authFetch(`${API}/auth/level-overrides`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Failed");
      }
      setSuccess(`Override granted: ${selectedUser.email} → Level ${grantLevel}`);
      resetGrantForm();
      loadOverrides();
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  }

  async function revokeOverride(overrideId, userEmail) {
    const ok = await confirm({ title: "Revoke Override", message: `Revoke override for ${userEmail}? Their AD level will be restored to the org tree value.`, confirmLabel: "Revoke" });
    if (!ok) return;
    setError(""); setSuccess("");
    try {
      const res = await authFetch(`${API}/auth/level-overrides/${overrideId}`, { method: "DELETE" });
      if (!res.ok) { const e = await res.json(); throw new Error(e.detail || "Failed"); }
      setSuccess(`Override revoked for ${userEmail}`);
      loadOverrides();
    } catch (e) {
      setError(e.message);
    }
  }

  async function loadHistory(userId) {
    const uid = userId || historyUserId;
    if (!uid) return;
    setHistory([]);
    setError("");
    try {
      const res  = await authFetch(`${API}/auth/level-overrides/user/${uid}`);
      if (!res.ok) { const e = await res.json(); throw new Error(e.detail || "Failed to load history"); }
      const data = await res.json();
      const piiOn = await piiEnabledPromise.current;
      const decrypted = await Promise.all((data.history || []).map(async h => ({
        ...h,
        grantor_email: await decryptPii(h.grantor_email, piiOn),
      })));
      setHistory(decrypted);
    } catch (e) { setError(e.message || "Failed to load history"); }
  }

  // Minimum level a director can grant (must be >= granter's own level).
  // Not used to filter the dropdown in flat mode (see FLAT_LEVEL_LABELS use
  // below) since flat mode is admin-only there anyway.
  const minGrantLevel = isAdmin ? 0 : adLevel;
  const levelOptions = hodMode ? LEVEL_LABELS : FLAT_LEVEL_LABELS;

  return (
    <>
    <div className="flex flex-col">
    {/* Header */}
    <div className="border-b border-gray-200 p-4 flex items-center justify-between">
       <div className="flex items-center gap-3">
          <UserCircle size={18} className="text-indigo-600"/>
          <div>
            <h1 className="text-sm font-semibold  text-indigo-700">Level Overrides</h1>
            <p className="text-xs text-gray-400">
             Temporarily promote a user's effective AD level. Overrides survive nightly org sync.
            {hodMode
              ? ` You can only grant levels ≥ your own (L${adLevel}).`
              : " In this deployment, admins can grant Elevated or Standard access."}
            </p>
          </div>
        </div>
      </div>
      </div>
    <div className="p-6">
      {error   && <div className="mb-3 max-w-2xl p-2 bg-red-50 border border-red-200 text-red-700 text-xs rounded">{error}</div>}
      {success && <div className="mb-3 max-w-2xl p-2 bg-green-50 border border-green-200 text-green-700 text-xs rounded">{success}</div>}

      {/* Tabs */}
      <div className="flex gap-2 mb-4 border-b border-gray-200">
        {["active", "grant", "history"].map(t => (
          <button
            key={t}
            onClick={() => { setActiveTab(t); setFormErrors(LO_EMPTY_ERRORS); setError(""); setSuccess(""); }}
            className={`pb-2 px-3 text-sm font-medium capitalize transition cursor-pointer ${
              activeTab === t ? "border-b-2 border-indigo-600 text-indigo-700" : "text-gray-400 hover:text-gray-600"
            }`}
          >
            {t === "active" ? `Active (${overrides.length})` : t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {/* ── Active Overrides Tab ────────────────────────────── */}
      {activeTab === "active" && (
        <div>
          {loading ? (
            <div className="text-xs text-gray-400">Loading...</div>
          ) : overrides.length === 0 ? (
            <div className="text-xs text-gray-400 py-4">No active overrides.</div>
          ) : (
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-b border-gray-200 text-left text-gray-500">
                  <th className="py-2 pr-3">User</th>
                  <th className="py-2 pr-3">Dept</th>
                  <th className="py-2 pr-3">Override Level</th>
                  <th className="py-2 pr-3">Original</th>
                  <th className="py-2 pr-3">Granted By</th>
                  <th className="py-2 pr-3">Reason</th>
                  <th className="py-2 pr-3">Expires</th>
                  <th className="py-2"></th>
                </tr>
              </thead>
              <tbody>
                {overrides.map(ov => (
                  <tr key={ov.id} className="border-b border-gray-100 hover:bg-gray-50">
                    <td className="py-2 pr-3 font-medium">{ov.user_email}<br/><span className="text-gray-400">{ov.user_name}</span></td>
                    <td className="py-2 pr-3 text-gray-500">{ov.department || '—'}</td>
                    <td className="py-2 pr-3">
                      <span className="px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-[10px] font-medium">
                        L{ov.ad_level_override}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-gray-400">L{ov.original_level ?? "?"}</td>
                    <td className="py-2 pr-3 text-gray-500">{ov.grantor_email}</td>
                    <td className="py-2 pr-3 max-w-[150px] truncate" title={ov.reason}>{ov.reason}</td>
                    <td className="py-2 pr-3 text-gray-400">
                      {ov.expires_at ? formatExpiryLiteral(ov.expires_at) : "Permanent"}
                    </td>
                    <td className="py-2">
                      <button
                        onClick={() => revokeOverride(ov.id, ov.user_email)}
                        className="text-red-500 hover:text-red-700 text-[10px] font-medium px-2 py-0.5 border border-red-200 rounded hover:bg-red-50 cursor-pointer"
                      >
                        Revoke
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* ── Grant Override Tab ──────────────────────────────── */}
      {activeTab === "grant" && (
        <form onSubmit={grantOverride} className="space-y-4 max-w-lg" noValidate>
          {/* User search */}
          <div className="relative">
            <label className="block text-xs font-medium text-gray-700 mb-1">Search User (email)</label>
            <input
              type="text"
              value={searchEmail}
              onChange={e => handleSearchChange(e.target.value)}
              onBlur={e => handleBlur("searchEmail", e.target.value)}
              placeholder="Type name or email to search..."
              className={`w-full px-3 py-1.5 text-sm border rounded focus:outline-none focus:border-indigo-300 ${formErrors.searchEmail ? "border-red-500" : "border-gray-300"}`}
            />
            {formErrors.searchEmail && <p className="mt-1 text-xs text-red-600">{formErrors.searchEmail}</p>}
            {searchResults.length > 0 && !selectedUser && (
              <div className="absolute z-10 w-full border border-gray-200 rounded mt-1 bg-white shadow-md max-h-40 overflow-y-auto">
                {searchResults.map(u => (
                  <button
                    key={u.id}
                    type="button"
                    onClick={() => { setSelectedUser(u); setSearchEmail(u.email); setSearchResults([]); setFormErrors(prev => ({ ...prev, searchEmail: "" })); }}
                    className="w-full text-left px-3 py-2 text-xs hover:bg-gray-50 border-b border-gray-100 last:border-0"
                  >
                    <span className="font-medium">{u.email}</span>
                    <span className="text-gray-400 ml-2">{u.name} · L{u.ad_level ?? 6}</span>
                  </button>
                ))}
              </div>
            )}
            {selectedUser && (
              <button type="button" onClick={() => { setSelectedUser(null); setSearchEmail(""); setSearchResults([]); }} className="absolute right-2 top-[30px] text-gray-400 hover:text-gray-600 text-sm leading-none">✕</button>
            )}
          </div>

          {/* Level select */}
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">Grant Level</label>
            <select
              value={grantLevel}
              onChange={e => setGrantLevel(parseInt(e.target.value))}
              className="w-full px-3 py-1.5 text-sm border border-gray-300 rounded focus:outline-none focus:border-indigo-300"
            >
              {Object.entries(levelOptions)
                .filter(([lvl]) => hodMode ? parseInt(lvl) >= minGrantLevel : true)
                .map(([lvl, label]) => (
                  <option key={lvl} value={lvl}>{label}</option>
                ))
              }
            </select>
          </div>

          {/* Reason */}
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">Reason <span className="text-red-400">*</span></label>
            <textarea
              value={grantReason}
              onChange={e => handleChange("grantReason", e.target.value, setGrantReason)}
              onBlur={e => handleBlur("grantReason", e.target.value)}
              rows={2}
              placeholder="e.g. Covering for manager during leave, Q1 release lead access"
              className={`w-full px-3 py-1.5 text-sm border rounded focus:outline-none focus:border-indigo-300 resize-none ${formErrors.grantReason ? "border-red-500" : "border-gray-300"}`}
            />
            {formErrors.grantReason && <p className="mt-1 text-xs text-red-600">{formErrors.grantReason}</p>}
          </div>

          {/* Expiry */}
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">Expires (optional — leave blank for permanent)</label>
            <input
              type="datetime-local"
              value={grantExpiry}
              onChange={e => setGrantExpiry(e.target.value)}
              className="w-full px-3 py-1.5 text-sm border border-gray-300 rounded focus:outline-none focus:border-indigo-300"
            />
          </div>

          <div className="flex gap-3">
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 brand-grad hover:opacity-70 text-white text-sm rounded cursor-pointer"
            >
              {submitting ? "Granting..." : "Grant Override"}
            </button>
            <button
              type="button"
              onClick={resetGrantForm}
              disabled={submitting}
              className="px-4 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 text-sm rounded cursor-pointer"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* ── History Tab ─────────────────────────────────────── */}
      {activeTab === "history" && (
        <div>
          <div className="flex gap-2 mb-4 flex-col max-w-xl">
            {/* Email search — same pattern as Grant tab */}
            <div className="relative">
              <label className="block text-xs font-medium text-gray-700 mb-1">Search User (email or name)</label>
              <input
                type="text"
                value={historySearchEmail}
                onChange={e => handleHistorySearchChange(e.target.value)}
                placeholder="Type name or email to search..."
                className="w-full px-3 py-1.5 text-sm border border-gray-300 rounded focus:outline-none focus:border-indigo-300"
              />
              {/* Dropdown results */}
              {historySearchResults.length > 0 && !historySelectedUser && (
                <div className="absolute z-10 w-full border border-gray-200 rounded mt-1 bg-white shadow-md max-h-40 overflow-y-auto">
                  {historySearchResults.map(u => (
                    <button
                      key={u.id}
                      type="button"
                      onClick={() => {
                        setHistorySelectedUser(u);
                        setHistorySearchEmail(u.email);
                        setHistoryUserId(u.id);
                        setHistorySearchResults([]);
                        loadHistory(u.id);
                      }}
                      className="w-full text-left px-3 py-2 text-xs hover:bg-gray-50 border-b border-gray-100 last:border-0"
                    >
                      <span className="font-medium">{u.email}</span>
                      <span className="text-gray-400 ml-2">{u.name} · L{u.ad_level ?? 6}</span>
                    </button>
                  ))}
                </div>
              )}
              {/* Clear selected user */}
              {historySelectedUser && (
                <button
                  type="button"
                  onClick={() => {
                    setHistorySelectedUser(null);
                    setHistorySearchEmail("");
                    setHistoryUserId("");
                    setHistorySearchResults([]);
                    setHistory([]);
                  }}
                  className="absolute right-2 top-[30px] text-gray-400 hover:text-gray-600 text-sm leading-none"
                >✕</button>
              )}
            </div>
          </div>
          {history.length === 0 ? (
            <div className="text-xs text-gray-400">{historySelectedUser ? "No override history found for this user." : "Search for a user above to view their override history."}</div>
          ) : (
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-b border-gray-200 text-left text-gray-500">
                  <th className="py-2 pr-3">Override Level</th>
                  <th className="py-2 pr-3">Original</th>
                  <th className="py-2 pr-3">Granted By</th>
                  <th className="py-2 pr-3">Reason</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Expires</th>
                  <th className="py-2 pr-3">Created</th>
                </tr>
              </thead>
              <tbody>
                {history.map(h => {
                  // Determine the correct status for this history entry.
                  // is_active in the DB may still be True for an expired override
                  // (the backend flips it lazily on the next Active-tab load).
                  // So we derive the display status here on the frontend:
                  //   • revoked_at set           → "Revoked"  (manually revoked)
                  //   • expires_at < now (IST)   → "Expired"  (time-based expiry)
                  //   • is_active = true         → "Active"
                  //   • fallback                 → "Expired"
                  // expires_at is stored as literal IST wall-clock digits (no UTC
                  // conversion), so we compare against IST "now" (UTC + 5:30).
                  let statusLabel, statusCls;
                  if (h.revoked_at) {
                    statusLabel = "Revoked";
                    statusCls   = "bg-gray-100 text-gray-500";
                  } else if (h.expires_at) {
                    // expires_at is stored as a naive IST wall-clock string with NO
                    // timezone suffix (e.g. "2026-08-05T15:30:00"). JS's Date() parses
                    // a tz-less ISO string as UTC, which would add an unwanted +5:30
                    // shift. Appending "+05:30" forces it to be interpreted as IST —
                    // consistent with how the backend stores and compares it.
                    const expiry  = new Date(h.expires_at + "+05:30");
                    const nowIST  = new Date();
                    if (expiry < nowIST) {
                      statusLabel = "Expired";
                      statusCls   = "bg-orange-100 text-orange-600";
                    } else if (h.is_active) {
                      statusLabel = "Active";
                      statusCls   = "bg-green-100 text-green-700";
                    } else {
                      statusLabel = "Expired";
                      statusCls   = "bg-orange-100 text-orange-600";
                    }
                  } else if (h.is_active) {
                    statusLabel = "Active";
                    statusCls   = "bg-green-100 text-green-700";
                  } else {
                    statusLabel = "Expired";
                    statusCls   = "bg-orange-100 text-orange-600";
                  }
                  return (
                  <tr key={h.id} className="border-b border-gray-100">
                    <td className="py-2 pr-3">L{h.ad_level_override}</td>
                    <td className="py-2 pr-3 text-gray-400">L{h.original_level ?? "?"}</td>
                    <td className="py-2 pr-3">{h.grantor_email}</td>
                    <td className="py-2 pr-3 max-w-[160px] truncate" title={h.reason}>{h.reason}</td>
                    <td className="py-2 pr-3">
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${statusCls}`}>
                        {statusLabel}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-gray-400">
                      {h.expires_at ? formatExpiryLiteral(h.expires_at) : "Permanent"}
                    </td>
                    <td className="py-2 pr-3 text-gray-400">{toISTDate(h.created_at)}</td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
    </>
  );
}
