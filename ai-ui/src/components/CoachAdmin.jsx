// SPDX-License-Identifier: MIT
// =============================================================================
// AiNxt Coach — Admin panel  (A1 · A3 · B1 · C1 · C2 · E3 · G1 · G3)
//
// Design language matches Coach.jsx exactly:
//   white cards · rounded-2xl · slate-200 border · shadow-[0_1px_2px_…]
//   text-[12px] font-bold titles · text-[10px] uppercase tracking-widest labels
//   accent left-border via before: pseudo-element
//
// Mounted as an admin-only tab inside Coach.jsx — no other file modified.
// Backend: routers/coach_admin_router.py
// Future features: docs/COACH_ADMIN_FUTURE_FEATURES.md
// =============================================================================
import { useEffect, useMemo, useRef, useState } from "react";
import { validateFreeText } from "../utils/securityValidation";
import { authFetch, apiFetch, MODEL_DEFAULT } from "../config";
import { decryptPii } from "../utils/piiCrypto";

// PII payload encryption flag (core/pii_crypto.py) — module-level singleton
// promise, fetched once from the unauthenticated /auth/ui-config endpoint.
// Used to decrypt "pii:v1:" email/name fields returned by /auth/users and
// /coach/admin/* endpoints BEFORE they are used as identifiers (e.g. the
// picked user's email is sent back as `user_id` in POST bodies — ciphertext
// there would fail backend lookup by User.email).
let _coachPiiEnabledPromise = null;
function coachPiiEnabled() {
  if (!_coachPiiEnabledPromise) {
    _coachPiiEnabledPromise = apiFetch(`/auth/ui-config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => !!d?.pii_payload_encryption_enabled)
      .catch(() => false);
  }
  return _coachPiiEnabledPromise;
}
import {
  Activity, AlertTriangle, BarChart2, Building2, CheckCircle, CheckCircle2,
  ChevronRight, FlaskConical, Mail, MessageSquarePlus, RefreshCw, ScrollText,
  Search, ShieldAlert, ShieldCheck, ShieldOff, Trash2,
  TrendingUp, User, X, Zap,
} from "lucide-react";

// ─────────────────────────────────────────────────────────────────────────────
// Shared utilities  (same patterns as Coach.jsx)
// ─────────────────────────────────────────────────────────────────────────────
async function jsonOrThrow(res) {
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error(data?.detail || data?.error || res.statusText || "Request failed");
  return data;
}

function useAdminFetch(url, deps = []) {
  const [data, setData]   = useState(null);
  const [err,  setErr]    = useState("");
  const [key,  bump]      = useState(0);
  useEffect(() => {
    const ac = new AbortController();
    setErr("");
    authFetch(url, { signal: ac.signal })
      .then(jsonOrThrow)
      .then(d => { if (!ac.signal.aborted) setData(d); })
      .catch(e => { if (e.name !== "AbortError") setErr(e.message); });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, key, ...deps]);
  return { data, err, reload: () => bump(k => k + 1) };
}

function useAdminAction(send) {
  const [busy, setBusy] = useState(false);
  const [out,  setOut]  = useState(null);
  const [err,  setErr]  = useState("");
  const run = async (...args) => {
    setBusy(true); setErr(""); setOut(null);
    try   { setOut(await send(...args)); }
    catch (e) { setErr(e.message); }
    finally   { setBusy(false); }
  };
  return { run, busy, out, err, reset: () => { setOut(null); setErr(""); } };
}

// ─────────────────────────────────────────────────────────────────────────────
// UserPicker — debounced autocomplete against GET /auth/users?search=
// Each card has its own independent instance; no cross-card state sharing.
// ─────────────────────────────────────────────────────────────────────────────
function UserPicker({ onChange, placeholder = "Search by name or email…" }) {
  const [query,   setQuery]   = useState("");
  const [picked,  setPicked]  = useState(null);
  const [results, setResults] = useState([]);
  const [open,    setOpen]    = useState(false);
  const [loading, setLoading] = useState(false);
  const timer   = useRef(null);
  const wrapRef = useRef(null);

  // Close on outside click.
  useEffect(() => {
    const h = e => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const search = q => {
    clearTimeout(timer.current);
    setQuery(q);
    if (!q.trim()) { setResults([]); setOpen(false); return; }
    timer.current = setTimeout(async () => {
      setLoading(true);
      try {
        const d = await jsonOrThrow(
          await authFetch(`/auth/users?search=${encodeURIComponent(q)}&page_size=8`)
        );
        const piiOn = await coachPiiEnabled();
        const decrypted = await Promise.all((d.users || []).map(async u => ({
          ...u,
          email: await decryptPii(u.email, piiOn),
          name:  await decryptPii(u.name,  piiOn),
        })));
        setResults(decrypted);
        setOpen(true);
      } catch { setResults([]); }
      finally { setLoading(false); }
    }, 250);
  };

  const pick = u => {
    setPicked(u);
    setQuery(u.email);
    setOpen(false);
    setResults([]);
    onChange(u);
  };

  // Full clear — resets both internal state AND notifies parent.
  const clear = () => {
    setPicked(null);
    setQuery("");
    setOpen(false);
    setResults([]);
    onChange(null);
  };

  return (
    <div ref={wrapRef} className="relative w-full">
      {/* Input */}
      <div className="relative">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
        <input
          value={query}
          onChange={e => { setPicked(null); search(e.target.value); }}
          onFocus={() => query && results.length && setOpen(true)}
          placeholder={placeholder}
          className="w-full pl-8 pr-7 py-1.5 text-[11.5px] border border-slate-200 rounded-lg bg-white
                     focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300
                     placeholder:text-slate-400 transition shadow-sm"
        />
        {query && (
          <button onClick={clear}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 transition">
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      {/* Selected chip — only shown when a user is actually picked */}
      {picked && (
        <div className="mt-1.5 flex items-center gap-1.5 px-2.5 py-1.5 bg-indigo-50/80
                        border border-indigo-200 rounded-lg text-[11px] text-indigo-800">
          <User className="w-3 h-3 flex-shrink-0 text-indigo-400" />
          <span className="font-medium truncate">{picked.email}</span>
          {picked.name && <span className="text-indigo-400 truncate">· {picked.name}</span>}
          {picked.department && (
            <span className="ml-auto text-[10px] bg-indigo-100 text-indigo-600 px-1.5 py-0.5 rounded-full flex-shrink-0">
              {picked.department}
            </span>
          )}
          <button onClick={clear} className="ml-1 text-indigo-400 hover:text-indigo-600 transition flex-shrink-0">
            <X className="w-3 h-3" />
          </button>
        </div>
      )}

      {/* Dropdown */}
      {open && (
        <div className="absolute z-50 mt-1 w-full bg-white border border-slate-200 rounded-xl
                        shadow-[0_4px_16px_rgba(15,23,42,0.10)] overflow-hidden max-h-52 overflow-y-auto">
          {loading && (
            <div className="px-3 py-2 text-[11px] text-slate-500 flex items-center gap-2">
              <RefreshCw className="w-3 h-3 animate-spin" /> Searching…
            </div>
          )}
          {!loading && results.length === 0 && (
            <div className="px-3 py-2 text-[11px] text-slate-500">No users found.</div>
          )}
          {results.map(u => (
            <button key={u.id} onClick={() => pick(u)}
                    className="w-full text-left px-3 py-2 hover:bg-indigo-50/60 transition
                               flex items-center gap-2.5 border-b border-slate-50 last:border-0">
              <div className="w-6 h-6 rounded-full brand-grad-vivid
                              flex items-center justify-center text-white text-[10px] font-bold flex-shrink-0">
                {(u.name || u.email || "?")[0].toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-[11.5px] font-medium text-slate-800 truncate">{u.email}</div>
                {u.name && <div className="text-[10px] text-slate-500 truncate">{u.name}</div>}
              </div>
              {u.department && (
                <span className="text-[10px] bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded-full flex-shrink-0">
                  {u.department}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Design primitives — matching Coach.jsx exactly
// ─────────────────────────────────────────────────────────────────────────────

// Same Card as Coach.jsx (accent left-border, white bg, rounded-2xl)
function AdminCard({ title, subtle, accent, children }) {
  const accentBar = {
    indigo: "before:bg-indigo-400",
    amber:  "before:bg-amber-400",
    red:    "before:bg-red-400",
    emerald:"before:bg-emerald-400",
    slate:  "before:bg-slate-400",
  }[accent] || "";
  return (
    <div className={`relative p-5 bg-white border border-slate-200 rounded-2xl h-full max-h-[520px] flex flex-col
                     shadow-[0_1px_2px_rgba(15,23,42,0.04)] ${
      accent
        ? `before:content-[''] before:absolute before:top-3 before:bottom-3 before:left-0
           before:w-0.5 before:rounded-r ${accentBar}`
        : ""
    }`}>
      {(title || subtle) && (
        <div className="mb-4 flex-shrink-0">
          {title && <div className="text-[12px] font-bold text-slate-800 tracking-tight">{title}</div>}
          {subtle && <div className="text-[10.5px] text-slate-400 mt-0.5 leading-snug">{subtle}</div>}
        </div>
      )}
      <div className="flex-1 min-h-0 overflow-y-auto pr-1">{children}</div>
    </div>
  );
}

// Same Stat tile as Coach.jsx
function StatTile({ label, value, icon: Icon, accent }) {
  const ACCENTS = {
    amber:  { bg: "bg-amber-50/60",   icon: "text-amber-500"  },
    indigo: { bg: "bg-indigo-50/60",  icon: "text-indigo-500" },
    red:    { bg: "bg-red-50/60",     icon: "text-red-500"    },
    emerald:{ bg: "bg-emerald-50/60", icon: "text-emerald-500"},
  };
  const a = ACCENTS[accent];
  return (
    <div className="relative overflow-hidden p-4 bg-white border border-slate-200 rounded-2xl
                    shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
      {a && <div className={`absolute inset-0 ${a.bg}`} />}
      <div className="relative">
        <div className="flex items-center justify-between">
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold">{label}</div>
          {Icon && <Icon size={14} className={a ? a.icon : "text-slate-300"} />}
        </div>
        <div className="text-2xl font-bold text-slate-900 mt-2 leading-none tracking-tight">{value}</div>
      </div>
    </div>
  );
}

function FieldLabel({ children }) {
  return (
    <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold mb-1.5">
      {children}
    </div>
  );
}

function AdminInput({ label, ...props }) {
  return (
    <div>
      {label && <FieldLabel>{label}</FieldLabel>}
      <input
        {...props}
        className="w-full px-2.5 py-1.5 text-[11.5px] border border-slate-200 rounded-lg bg-white
                   focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300
                   placeholder:text-slate-400 transition shadow-sm"
      />
    </div>
  );
}

function AdminSelect({ label, children, ...props }) {
  return (
    <div>
      {label && <FieldLabel>{label}</FieldLabel>}
      <div className="relative">
        <select
          {...props}
          className="w-full appearance-none px-2.5 py-1.5 pr-7 text-[11.5px] border border-slate-200
                     rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-indigo-200
                     focus:border-indigo-300 transition shadow-sm cursor-pointer"
        >
          {children}
        </select>
        <ChevronRight className="absolute right-2 top-1/2 -translate-y-1/2 w-3 h-3 text-slate-400
                                  pointer-events-none rotate-90" />
      </div>
    </div>
  );
}

// Matches the button style used in Coach.jsx event rows
function AdminBtn({ onClick, disabled, busy, busyLabel, children, variant = "primary" }) {
  const styles = {
    primary:   "bg-indigo-600 hover:bg-indigo-700 text-white border-transparent",
    danger:    "bg-red-600 hover:bg-red-700 text-white border-transparent",
    amber:     "bg-amber-500 hover:bg-amber-600 text-white border-transparent",
    emerald:   "bg-emerald-600 hover:bg-emerald-700 text-white border-transparent",
    secondary: "bg-white hover:bg-slate-50 text-slate-700 border-slate-200",
  };
  return (
    <button
      onClick={onClick}
      disabled={disabled || busy}
      className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-medium
                  rounded-lg border transition focus:outline-none focus:ring-2 focus:ring-offset-1
                  focus:ring-indigo-300 disabled:opacity-40 disabled:cursor-not-allowed
                  ${styles[variant]}`}
    >
      {busy
        ? <><RefreshCw className="w-3 h-3 animate-spin" />{busyLabel || "Working…"}</>
        : children}
    </button>
  );
}

function ErrorBox({ msg }) {
  if (!msg) return null;
  return (
    <div className="mt-3 p-3 bg-red-50 border border-red-200 rounded-xl flex items-center gap-2
                    text-red-700 text-[11px]">
      <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" /> {msg}
    </div>
  );
}

function SuccessBox({ msg }) {
  if (!msg) return null;
  return (
    <div className="mt-3 p-3 bg-emerald-50 border border-emerald-200 rounded-xl flex items-center gap-2
                    text-emerald-700 text-[11px]">
      <CheckCircle2 className="w-3.5 h-3.5 flex-shrink-0" /> {msg}
    </div>
  );
}

function AdminSkeleton() {
  return (
    <div className="space-y-2 animate-pulse">
      <div className="h-8 bg-gradient-to-r from-slate-100 to-slate-50 rounded-xl" />
      <div className="h-8 bg-gradient-to-r from-slate-100 to-slate-50 rounded-xl opacity-70" />
      <div className="h-8 bg-gradient-to-r from-slate-100 to-slate-50 rounded-xl opacity-40" />
    </div>
  );
}

function EmptyState({ icon: Icon = CheckCircle2, text }) {
  return (
    <div className="flex flex-col items-center justify-center py-8 text-slate-400 gap-2">
      <Icon className="w-7 h-7 text-slate-300" />
      <p className="text-[11px] italic">{text}</p>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Root
// ─────────────────────────────────────────────────────────────────────────────
export default function CoachAdmin({ user, days = 30 }) {
  const isAdmin = (user?.role || "").toLowerCase() === "admin";

  if (!isAdmin) {
    return (
      <div className="flex items-center justify-center h-48 text-[12px] text-slate-500 gap-2">
        <ShieldAlert className="w-4 h-4 text-slate-400" /> Admin role required.
      </div>
    );
  }

  return (
    <div className="space-y-5 max-w-6xl mx-auto">

      {/* ── Row 1: Insight ───────────────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-stretch">
        <ImpactCard days={days} />
        <AttentionCard days={days} />
      </div>

      {/* ── Row 2: User actions ──────────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-stretch">
        <ResetCard />
        <PurgeCard />
      </div>

      {/* ── Row 3: Rules + Coaching ──────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-stretch">
        <RulesCard />
        <ManualCoachCard days={days} />
      </div>

      {/* ── Row 4: Analytics ─────────────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-stretch">
        <CostVsPracticeCard days={days} />
        <AuditCard days={days} />
      </div>

      {/* ── Row 5: Rule Playground (FR-IMP-2) ────────────────────────── */}
      <PlaygroundCard />

      {/* ── Row 6: Weekly digest email management ────────────────────── */}
      <WeeklyMailCard />

      {/* ── Row 7: Department breakdown (replaces Org Rollups tab) ───── */}
      <DeptBreakdownSection days={days} />

    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// G1 — Coach Impact
// ─────────────────────────────────────────────────────────────────────────────
function ImpactCard({ days }) {
  const { data, err } = useAdminFetch(`/coach/admin/impact?days=${days}`, [days]);

  return (
    <AdminCard title="Coach Impact" subtle={`How much coaching happened and what it cost · last ${days} days`} accent="indigo">
      {!data && !err && <AdminSkeleton />}
      {err && <ErrorBox msg={err} />}
      {data && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-4">
            <StatTile label="Events"        value={data.events}                accent="indigo" icon={Activity} />
            <StatTile label="Rule hits"     value={data.rule_hits}             accent="amber"  icon={AlertTriangle} />
            <StatTile label="PII blocked"   value={data.pii_leaks_blocked}     accent="red"    icon={ShieldAlert} />
            <StatTile label="Vague coached" value={data.vague_prompts_coached}                 icon={Zap} />
            <StatTile label="Total spend"   value={`$${data.total_spend_usd}`} accent="emerald" icon={TrendingUp} />
          </div>
          {data.hits_by_category?.length > 0 && (
            <>
              <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold mb-2">
                Hits by category
              </div>
              <div className="flex flex-wrap gap-1.5">
                {data.hits_by_category.map(r => (
                  <span key={r.category}
                        className="text-[10.5px] bg-slate-100 text-slate-600 rounded-full px-2.5 py-0.5
                                   border border-slate-200">
                    {r.category} <span className="font-semibold text-slate-800">{r.count}</span>
                  </span>
                ))}
              </div>
            </>
          )}
        </>
      )}
    </AdminCard>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// C1 — Users needing attention
// ─────────────────────────────────────────────────────────────────────────────
function AttentionCard({ days }) {
  const { data, err, reload } = useAdminFetch(`/coach/admin/attention?days=${days}&limit=10`, [days]);
  const [rows, setRows] = useState([]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const piiOn = await coachPiiEnabled();
      const decrypted = await Promise.all((data?.items || []).map(async r => ({
        ...r,
        email: await decryptPii(r.email, piiOn),
        name:  await decryptPii(r.name,  piiOn),
      })));
      if (!cancelled) setRows(decrypted);
    })();
    return () => { cancelled = true; };
  }, [data]);

  return (
    <AdminCard title="Users Needing Attention"
               subtle={`Users with the most rule violations this period — good starting point for a coaching conversation · last ${days} days`} accent="amber">
      <div className="flex items-center justify-between mb-3">
        <span className="text-[10.5px] text-slate-400 italic">
          Users with the highest violation count this window.
        </span>
        <button onClick={reload}
                className="text-[11px] px-2 py-1 rounded-lg bg-slate-100 hover:bg-slate-200
                           text-slate-600 flex items-center gap-1 transition">
          <RefreshCw className="w-3 h-3" /> Refresh
        </button>
      </div>

      {!data && !err && <AdminSkeleton />}
      {err && <ErrorBox msg={err} />}
      {data && rows.length === 0 && (
        <EmptyState icon={CheckCircle2} text="No problem signals this window — all quiet." />
      )}
      {rows.length > 0 && (
        <div className="bg-white border border-slate-200 rounded-2xl shadow-[0_1px_2px_rgba(15,23,42,0.04)] overflow-hidden max-h-72">
          <table className="w-full text-[11px]">
            <thead>
              <tr className="text-slate-400 uppercase text-[9.5px] tracking-widest font-semibold
                             bg-slate-50/70 border-b border-slate-100">
                <th className="px-3 py-2 text-left">User</th>
                <th className="px-3 py-2 text-right">Hits</th>
                <th className="px-3 py-2 text-right">Crit</th>
                <th className="px-3 py-2 text-right">PII</th>
                <th className="px-3 py-2 text-left">Top rule</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.user_id}
                    className="border-b border-slate-50 hover:bg-slate-50/60 transition">
                  <td className="px-3 py-2">
                    <div className="font-medium text-slate-800 truncate max-w-[160px]">
                      {r.email || r.name || r.user_id}
                    </div>
                    {r.department && (
                      <div className="text-[10px] text-slate-400">{r.department}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right font-semibold text-slate-700">{r.hits}</td>
                  <td className="px-3 py-2 text-right">
                    {r.critical > 0
                      ? <span className="font-semibold text-red-600">{r.critical}</span>
                      : <span className="text-slate-300">—</span>}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {r.pii_events > 0
                      ? <span className="font-semibold text-amber-600">{r.pii_events}</span>
                      : <span className="text-slate-300">—</span>}
                  </td>
                  <td className="px-3 py-2">
                    {r.top_rule
                      ? <code className="text-[10px] bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded font-mono">
                          {r.top_rule.rule_id} ×{r.top_rule.n}
                        </code>
                      : <span className="text-slate-300">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </AdminCard>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// A1 — Reset score
// ─────────────────────────────────────────────────────────────────────────────
function ResetCard() {
  const [pickedUser, setPickedUser] = useState(null);
  const [days,     setDays]     = useState(30);
  const [category, setCategory] = useState("");
  const [mode,     setMode]     = useState("soft");
  const [reason,   setReason]   = useState("");

  const action = useAdminAction(async () => {
    if (!pickedUser) throw new Error("Please select a user first.");
    return jsonOrThrow(await authFetch(`/coach/admin/reset`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: pickedUser.email || pickedUser.id,
        days: Number(days), category: category || null, mode, reason,
      }),
    }));
  });

  return (
    <AdminCard title="Reset User Score"
               subtle="Give a user a fresh start — either hide their past violations from the score (soft) or permanently delete them (hard). Use soft for onboarding or after a coaching session; hard only for data corrections." accent="indigo">
      <div className="space-y-3">
        <div>
          <FieldLabel>User</FieldLabel>
          <UserPicker onChange={setPickedUser} />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <AdminInput label="Window (days)" type="number" value={days}
                      onChange={e => setDays(Number(e.target.value) || 0)} />
          <AdminInput label="Category (optional)" value={category}
                      onChange={e => setCategory(e.target.value)} placeholder="e.g. security" />
          <AdminSelect label="Mode" value={mode} onChange={e => setMode(e.target.value)}>
            <option value="soft">Soft — mute hits (keeps audit trail)</option>
            <option value="hard">Hard — delete hits permanently</option>
          </AdminSelect>
          <AdminInput label="Reason" value={reason} onChange={e => setReason(e.target.value)}
                      placeholder="e.g. onboarding fresh start" />
        </div>
        <AdminBtn onClick={() => action.run()} busy={action.busy} busyLabel="Resetting…"
                  variant={mode === "hard" ? "danger" : "primary"}>
          <RefreshCw className="w-3 h-3" />
          {mode === "hard" ? "Hard reset" : "Soft reset"}
        </AdminBtn>
        <ErrorBox msg={action.err} />
        <SuccessBox msg={action.out
          ? `✓ ${action.out.mode === "hard" ? "Deleted" : "Muted"} ${action.out.affected_hits} hit(s) for ${action.out.user_id}.`
          : ""} />
      </div>
    </AdminCard>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// A3 — Purge data
// ─────────────────────────────────────────────────────────────────────────────
function PurgeCard() {
  const [pickedUser, setPickedUser] = useState(null);
  const [days,   setDays]   = useState(180);
  const [reason, setReason] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);

  const action = useAdminAction(async () => {
    if (!pickedUser) throw new Error("Please select a user first.");
    return jsonOrThrow(await authFetch(`/coach/admin/purge`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: pickedUser.email || pickedUser.id, days: Number(days), reason }),
    }));
  });

  const label = pickedUser?.email || pickedUser?.id || "";

  return (
    <AdminCard title="Delete User's Coach History"
               subtle="Permanently removes a user's activity records older than the chosen number of days. Use this when a user requests their data be erased (GDPR / right-to-erasure) or for routine data housekeeping. This cannot be undone." accent="red">
      <div className="space-y-3">
        <div>
          <FieldLabel>User</FieldLabel>
          <UserPicker onChange={u => { setPickedUser(u); setConfirmOpen(false); }} />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <AdminInput label="Older than (days)" type="number" value={days}
                      onChange={e => { setDays(Number(e.target.value) || 0); setConfirmOpen(false); }} />
          <AdminInput label="Reason" value={reason} onChange={e => { setReason(e.target.value); setConfirmOpen(false); }}
                      placeholder="e.g. GDPR request" />
        </div>

        {/* In-card confirmation — replaces browser alert that exposed server details */}
        {confirmOpen && (
          <div className="p-3 bg-red-50 border border-red-200 rounded-xl space-y-2">
            <div className="flex items-start gap-2 text-[11px] text-red-800">
              <AlertTriangle className="w-4 h-4 flex-shrink-0 text-red-500" />
              <div>
                <p className="font-semibold">This action cannot be undone.</p>
                <p className="mt-0.5">
                  Permanently delete all coach events and rule hits older than <strong>{days}</strong> days
                  {label && <> for <strong>{label}</strong></>}?
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <AdminBtn onClick={() => { action.run().then(() => setConfirmOpen(false)); }} busy={action.busy} busyLabel="Deleting…" variant="danger">
                <Trash2 className="w-3 h-3" /> Yes, delete
              </AdminBtn>
              <AdminBtn onClick={() => setConfirmOpen(false)} variant="secondary" disabled={action.busy}>
                Cancel
              </AdminBtn>
            </div>
          </div>
        )}

        {!confirmOpen && (
          <AdminBtn onClick={() => setConfirmOpen(true)} variant="danger">
            <Trash2 className="w-3 h-3" /> Delete history
          </AdminBtn>
        )}

        {action.err && <ErrorBox msg={action.err} />}
        <SuccessBox msg={action.out
          ? `✓ ${action.out.events_deleted} events and ${action.out.hits_deleted} hits deleted.`
          : ""} />
      </div>
    </AdminCard>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// B1 — Rule kill-switch
// ─────────────────────────────────────────────────────────────────────────────
function RulesCard() {
  const [ruleId, setRuleId] = useState("");
  const [dept,   setDept]   = useState("");
  const [reason, setReason] = useState("");
  const { data, err: loadErr, reload } = useAdminFetch(`/coach/admin/rules/disabled`, []);
  const rows = data?.items || [];

  const disable = useAdminAction(async () => {
    if (!ruleId.trim()) throw new Error("Rule ID is required.");
    const d = await jsonOrThrow(await authFetch(`/coach/admin/rules/disable`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rule_id: ruleId.trim(), department: dept.trim() || null, reason }),
    }));
    setRuleId(""); setDept(""); setReason(""); reload();
    return d;
  });

  const enable = useAdminAction(async (rule_id, department) => {
    const d = await jsonOrThrow(await authFetch(`/coach/admin/rules/enable`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rule_id, department }),
    }));
    reload(); return d;
  });

  const err  = disable.err || enable.err || loadErr;
  const busy = disable.busy || enable.busy;

  return (
    <AdminCard title="Silence a Coaching Rule"
               subtle="Turn off a specific rule so it stops flagging users — useful when a rule is producing too many false positives. You can silence it for everyone or just one department, and re-enable it any time." accent="amber">
      <div className="space-y-3">
        {/* Info note */}
        <div className="flex items-start gap-2 px-3 py-2 bg-amber-50 border border-amber-200
                        rounded-xl text-[10.5px] text-amber-800">
          <AlertTriangle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0 text-amber-500" />
          <span>
            Disabling a rule is saved and logged immediately. The rule will stop scoring users once the
            one-line evaluator patch is applied (see{" "}
            <code className="font-mono bg-amber-100 px-1 rounded">COACH_ADMIN_FUTURE_FEATURES.md</code>).
          </span>
        </div>

        <div className="grid grid-cols-3 gap-2">
          <AdminInput label="Rule ID" value={ruleId} onChange={e => setRuleId(e.target.value)}
                      placeholder="AINXT-MS-001" />
          <AdminInput label="Department (blank = org-wide)" value={dept}
                      onChange={e => setDept(e.target.value)} placeholder="e.g. Compliance" />
          <AdminInput label="Reason" value={reason} onChange={e => setReason(e.target.value)}
                      placeholder="noisy false-positive" />
        </div>

        <AdminBtn onClick={() => disable.run()} busy={busy} busyLabel="Disabling…" variant="amber">
          <ShieldOff className="w-3 h-3" /> Disable rule
        </AdminBtn>

        <ErrorBox msg={err} />

        {/* Disabled rules list */}
        <div>
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold mb-2">
            Currently disabled
          </div>
          {!data && <AdminSkeleton />}
          {data && rows.length === 0 && (
            <div className="text-[11px] text-slate-400 italic py-1">No rules disabled.</div>
          )}
          <div className="space-y-1.5 max-h-44 overflow-y-auto">
            {rows.map(r => (
              <div key={r.id}
                   className="flex items-center justify-between px-3 py-2 bg-slate-50
                              border border-slate-200 rounded-xl text-[11px]">
                <div className="min-w-0">
                  <code className="font-mono font-semibold text-slate-800">{r.rule_id}</code>
                  <span className="ml-2 text-[10px] bg-slate-200 text-slate-600 px-1.5 py-0.5 rounded-full">
                    {r.department || "org-wide"}
                  </span>
                  {r.reason && (
                    <span className="ml-2 text-slate-400 italic">{r.reason}</span>
                  )}
                </div>
                <button onClick={() => enable.run(r.rule_id, r.department)}
                        className="ml-2 flex-shrink-0 flex items-center gap-1 text-emerald-600
                                   hover:text-emerald-700 text-[11px] font-medium transition">
                  <ShieldCheck className="w-3 h-3" /> Re-enable
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
    </AdminCard>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Task-type analysis panel — shown inline inside ManualCoachCard
// ─────────────────────────────────────────────────────────────────────────────
const DOMAIN_COLOURS = {
  code:     { bg: "bg-indigo-50",   border: "border-indigo-200",  text: "text-indigo-700",  bar: "#4f46e5" },
  devops:   { bg: "bg-violet-50",   border: "border-violet-200",  text: "text-violet-700",  bar: "#7c3aed" },
  data:     { bg: "bg-cyan-50",     border: "border-cyan-200",    text: "text-cyan-700",    bar: "#0891b2" },
  security: { bg: "bg-red-50",      border: "border-red-200",     text: "text-red-700",     bar: "#dc2626" },
  finance:  { bg: "bg-emerald-50",  border: "border-emerald-200", text: "text-emerald-700", bar: "#059669" },
  hr:       { bg: "bg-pink-50",     border: "border-pink-200",    text: "text-pink-700",    bar: "#db2777" },
  legal:    { bg: "bg-amber-50",    border: "border-amber-200",   text: "text-amber-700",   bar: "#d97706" },
  general:  { bg: "bg-slate-50",    border: "border-slate-200",   text: "text-slate-600",   bar: "#64748b" },
};

function TaskAnalysisPanel({ data, loading, err }) {
  const [expanded, setExpanded] = useState(null);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-[11px] text-slate-500 py-2">
        <RefreshCw className="w-3 h-3 animate-spin" /> Analysing task types…
      </div>
    );
  }
  if (err) return <ErrorBox msg={`Task analysis failed: ${err}`} />;
  if (!data) return null;

  const { total_events, domains = [], summary } = data;

  if (total_events === 0) {
    return (
      <div className="p-3 bg-slate-50 border border-slate-200 rounded-xl text-[11px] text-slate-500 italic">
        No activity found — task analysis requires at least one event in the selected window.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {/* Summary sentence */}
      <p className="text-[11.5px] text-slate-600 leading-relaxed">{summary}</p>

      {/* Domain bars */}
      <div className="space-y-2">
        {domains.map(d => {
          const c = DOMAIN_COLOURS[d.domain] || DOMAIN_COLOURS.general;
          const isOpen = expanded === d.domain;
          return (
            <div key={d.domain}
                 className={`border rounded-xl overflow-hidden transition-all ${c.border} ${c.bg}`}>
              {/* Header row — click to expand */}
              <button
                onClick={() => setExpanded(isOpen ? null : d.domain)}
                className="w-full flex items-center gap-3 px-3 py-2 text-left"
              >
                {/* Bar */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between mb-1">
                    <span className={`text-[11px] font-semibold ${c.text}`}>{d.label}</span>
                    <span className="text-[10px] text-slate-500 ml-2 flex-shrink-0">
                      {d.count} interaction{d.count !== 1 ? "s" : ""} · {d.pct}%
                    </span>
                  </div>
                  <div className="h-1.5 bg-white/60 rounded-full overflow-hidden border border-white/40">
                    <div
                      className="h-full rounded-full transition-all duration-500"
                      style={{ width: `${d.pct}%`, background: c.bar }}
                    />
                  </div>
                </div>
                <ChevronRight
                  className={`w-3.5 h-3.5 flex-shrink-0 ${c.text} transition-transform ${isOpen ? "rotate-90" : ""}`}
                />
              </button>

              {/* Expanded tips */}
              {isOpen && (
                <div className="px-3 pb-3 space-y-1.5 border-t border-white/50">
                  {d.top_issues.length === 0 ? (
                    <p className="text-[11px] text-emerald-600 pt-2">
                      ✓ No recurring issues detected for this task type — great work!
                    </p>
                  ) : (
                    d.top_issues.map((issue, i) => (
                      <div key={i} className="pt-2">
                        <div className="flex items-center gap-1.5 mb-0.5">
                          <span className={`text-[9.5px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-white/70 ${c.text}`}>
                            {issue.category}
                          </span>
                          <span className="text-[10px] text-slate-400">×{issue.count} hit{issue.count !== 1 ? "s" : ""}</span>
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
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// C2 — Manual coaching  (unified single-format send)
// ─────────────────────────────────────────────────────────────────────────────
function ManualCoachCard({ days = 30 }) {
  const [pickedUser,  setPickedUser]  = useState(null);
  const [customNote,  setCustomNote]  = useState("");   // optional admin note
  const [includeTA,   setIncludeTA]   = useState(false); // include task analysis
  // Preview state — populated from /preview-message
  const [preview,     setPreview]     = useState(null);
  const [previewErr,  setPreviewErr]  = useState("");
  const [previewing,  setPreviewing]  = useState(false);
  // Standalone task-analysis state (shown before preview is generated)
  const [taData,      setTaData]      = useState(null);
  const [taLoading,   setTaLoading]   = useState(false);
  const [taErr,       setTaErr]       = useState("");
  // Editable subject override
  const [subject,     setSubject]     = useState("");
  const [subjectEdited, setSubjectEdited] = useState(false);
  // View mode: "edit" (plain text) | "preview" (HTML iframe)
  const [viewMode,    setViewMode]    = useState("edit");

  // Fetch standalone task analysis — always uses the current days window
  const fetchTaskAnalysis = (user, d) => {
    if (!user) { setTaData(null); setTaErr(""); return; }
    setTaLoading(true); setTaErr("");
    authFetch(`/coach/admin/task-analysis?user_id=${encodeURIComponent(user.email || user.id)}&days=${d}`)
      .then(jsonOrThrow)
      .then(r => setTaData(r))
      .catch(e => setTaErr(e.message))
      .finally(() => setTaLoading(false));
  };

  // Re-fetch preview — always uses the current days window
  const fetchPreview = (user, note, ta, d) => {
    if (!user) { setPreview(null); setSubject(""); setSubjectEdited(false); setViewMode("edit"); return; }

    // Client-side pre-check — mirrors validate_coach_note_request() in
    // core/security_validation.py (XSS-only via validate_free_text() for
    // custom_note/subject). The backend (POST /coach/admin/preview-message)
    // remains the authoritative enforcer.
    if (note) {
      const noteCheck = validateFreeText(note);
      if (!noteCheck.isValid) {
        setPreviewErr(noteCheck.errors[0]?.message || "Invalid note");
        return;
      }
    }

    setPreviewing(true); setPreviewErr("");
    authFetch(`/coach/admin/preview-message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id:               user.email || user.id,
        kind:                  "coaching_note",
        custom_note:           note || "",
        include_task_analysis: ta,
        days:                  d,
      }),
    })
      .then(jsonOrThrow)
      .then(r => {
        setPreview(r);
        if (!subjectEdited) setSubject(r.subject);
      })
      .catch(e => setPreviewErr(e.message))
      .finally(() => setPreviewing(false));
  };

  // Re-fetch when user changes
  useEffect(() => {
    fetchPreview(pickedUser, customNote, includeTA, days);
    if (pickedUser) fetchTaskAnalysis(pickedUser, days);
    else { setTaData(null); setTaErr(""); }
  }, [pickedUser?.id]); // eslint-disable-line

  // Re-fetch when the global days window changes (user changed the 7d/30d/90d picker)
  useEffect(() => {
    if (!pickedUser) return;
    fetchPreview(pickedUser, customNote, includeTA, days);
    fetchTaskAnalysis(pickedUser, days);
  }, [days]); // eslint-disable-line

  // Debounce note changes (600 ms)
  useEffect(() => {
    if (!pickedUser) return;
    const t = setTimeout(() => fetchPreview(pickedUser, customNote, includeTA, days), 600);
    return () => clearTimeout(t);
  }, [customNote]); // eslint-disable-line

  // Re-fetch preview immediately when task-analysis toggle changes
  useEffect(() => {
    if (!pickedUser) return;
    fetchPreview(pickedUser, customNote, includeTA, days);
  }, [includeTA]); // eslint-disable-line

  const action = useAdminAction(async () => {
    if (!pickedUser) throw new Error("Please select a user first.");

    // Client-side pre-check — mirrors validate_coach_note_request() in
    // core/security_validation.py (XSS-only via validate_free_text() for
    // custom_note/subject). The backend (POST /coach/admin/coach-user)
    // remains the authoritative enforcer.
    if (customNote) {
      const noteCheck = validateFreeText(customNote);
      if (!noteCheck.isValid) throw new Error(noteCheck.errors[0]?.message || "Invalid note");
    }
    if (subjectEdited && subject) {
      const subjectCheck = validateFreeText(subject);
      if (!subjectCheck.isValid) throw new Error(subjectCheck.errors[0]?.message || "Invalid subject");
    }

    return jsonOrThrow(await authFetch(`/coach/admin/coach-user`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id:               pickedUser.email || pickedUser.id,
        kind:                  "coaching_note",
        custom_note:           customNote || undefined,
        subject:               subjectEdited ? subject : undefined,
        include_task_analysis: includeTA,
        days,
      }),
    }));
  });

  return (
    <AdminCard
      title="Send a Coaching Message"
      subtle={`Scores and recommendations are based on the last ${days} days — matching the time window selected above.`}
      accent="emerald"
    >
      <div className="space-y-3">

        {/* ── User picker ─────────────────────────────────────────────── */}
        <div>
          <FieldLabel>User</FieldLabel>
          <UserPicker onChange={u => {
            setPickedUser(u);
            setCustomNote("");
            setSubjectEdited(false);
            setPreview(null);
            setTaData(null);
            setTaErr("");
          }} />
        </div>

        {/* ── Task-type analysis toggle ────────────────────────────────── */}
        <div className={`flex items-start gap-3 px-3 py-2.5 rounded-xl border transition ${
          includeTA
            ? "bg-violet-50 border-violet-200"
            : "bg-slate-50 border-slate-200"
        }`}>
          <div className="flex items-center gap-2 flex-1 min-w-0">
            <BarChart2 className={`w-4 h-4 flex-shrink-0 ${includeTA ? "text-violet-600" : "text-slate-400"}`} />
            <div className="min-w-0">
              <div className={`text-[11.5px] font-semibold ${includeTA ? "text-violet-800" : "text-slate-700"}`}>
                Include task-type analysis
              </div>
              <div className="text-[10.5px] text-slate-500 leading-snug mt-0.5">
                Classifies the user's recent prompts by domain (coding, DevOps, data, etc.) and adds
                targeted improvement tips per task type to the message.
              </div>
            </div>
          </div>
          <button
            onClick={() => setIncludeTA(v => !v)}
            className={`relative flex-shrink-0 w-9 h-5 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-violet-300 ${
              includeTA ? "bg-violet-600" : "bg-slate-300"
            }`}
            role="switch"
            aria-checked={includeTA}
          >
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${
              includeTA ? "translate-x-4" : "translate-x-0"
            }`} />
          </button>
        </div>

        {/* ── Inline task analysis panel (shown when toggle is on and user is picked) */}
        {includeTA && pickedUser && (
          <div className="border border-violet-200 rounded-xl bg-violet-50/40 p-3">
            <div className="flex items-center gap-1.5 mb-2">
              <BarChart2 className="w-3.5 h-3.5 text-violet-600" />
              <span className="text-[10px] font-bold uppercase tracking-widest text-violet-700">
                Task-type breakdown
              </span>
            </div>
            <TaskAnalysisPanel data={taData} loading={taLoading} err={taErr} />
          </div>
        )}

        {/* ── Optional coach note ─────────────────────────────────────── */}
        <div>
          <FieldLabel>Personal note <span className="font-normal text-slate-400 normal-case">(optional — appended to the message)</span></FieldLabel>
          <textarea
            value={customNote}
            onChange={e => setCustomNote(e.target.value)}
            placeholder="Add a personal note for this user, e.g. 'Great progress this sprint — keep it up!'"
            rows={3}
            className="w-full px-2.5 py-1.5 text-[11.5px] border border-slate-200 rounded-lg bg-white
                       focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300
                       placeholder:text-slate-400 transition shadow-sm resize-none"
          />
        </div>

        {/* ── Loading / error states ───────────────────────────────────── */}
        {!pickedUser && (
          <div className="text-[11px] text-slate-400 italic py-1">
            Select a user above to generate a preview.
          </div>
        )}
        {pickedUser && previewing && (
          <div className="flex items-center gap-2 text-[11px] text-slate-500 py-1">
            <RefreshCw className="w-3 h-3 animate-spin" /> Generating from user data…
          </div>
        )}
        {previewErr && <ErrorBox msg={`Preview failed: ${previewErr}`} />}

        {/* ── Preview area ─────────────────────────────────────────────── */}
        {pickedUser && !previewing && preview && (
          <>
            {/* Subject line */}
            <div className="flex items-center justify-between">
              <FieldLabel>Subject</FieldLabel>
              {subjectEdited && (
                <button
                  onClick={() => { setSubject(preview.subject); setSubjectEdited(false); }}
                  className="text-[10px] text-indigo-500 hover:text-indigo-700 transition"
                >
                  ↺ Reset
                </button>
              )}
            </div>
            <input
              value={subject}
              onChange={e => { setSubject(e.target.value); setSubjectEdited(true); }}
              className="w-full px-2.5 py-1.5 text-[11.5px] border border-slate-200 rounded-lg bg-white
                         focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300
                         transition shadow-sm"
            />

            {/* Edit / HTML preview tabs */}
            <div className="flex items-center border-b border-slate-200">
              {[
                { key: "edit",    label: "✏️ Plain text" },
                { key: "preview", label: "👁 HTML preview" },
              ].map(tab => (
                <button
                  key={tab.key}
                  onClick={() => setViewMode(tab.key)}
                  className={`px-3 py-1.5 text-[11px] font-medium border-b-2 transition -mb-px ${
                    viewMode === tab.key
                      ? "border-indigo-500 text-indigo-700"
                      : "border-transparent text-slate-500 hover:text-slate-700"
                  }`}
                >
                  {tab.label}
                </button>
              ))}
              <span className="ml-auto flex items-center gap-1.5 pb-1">
                <span className="text-[10px] font-semibold bg-indigo-100 text-indigo-600 px-1.5 py-0.5 rounded-full">
                  last {days}d
                </span>
                <span className="text-[10px] text-emerald-600 italic">generated from user data</span>
              </span>
            </div>

            {viewMode === "edit" ? (
              <div className="p-3 bg-slate-50 border border-slate-200 rounded-lg text-[11.5px]
                              text-slate-700 font-mono whitespace-pre-wrap leading-relaxed max-h-52 overflow-y-auto">
                {preview.body || "(no body)"}
              </div>
            ) : preview.html_body ? (
              <div className="border border-slate-200 rounded-xl overflow-hidden shadow-sm">
                <div className="px-3 py-1.5 bg-slate-50 border-b border-slate-200">
                  <span className="text-[10px] font-semibold text-slate-500 uppercase tracking-wider">
                    📧 Email preview — exactly what the user will receive
                  </span>
                </div>
                <iframe
                  srcDoc={preview.html_body}
                  title="Email preview"
                  sandbox="allow-same-origin"
                  className="w-full border-0"
                  style={{ height: "420px" }}
                />
              </div>
            ) : (
              <div className="text-[11px] text-slate-400 italic py-4 text-center">
                HTML preview not available.
              </div>
            )}

            {/* Email destination note */}
            {preview.html_body && pickedUser?.email && (
              <div className="flex items-center gap-1.5 text-[10.5px] text-slate-500 bg-slate-50
                              border border-slate-200 rounded-lg px-3 py-2">
                <span>📧</span>
                <span>Will also send HTML email to <strong>{pickedUser.email}</strong></span>
              </div>
            )}

            <AdminBtn onClick={() => action.run()} busy={action.busy} busyLabel="Sending…">
              <MessageSquarePlus className="w-3 h-3" /> Send to {pickedUser.email}
            </AdminBtn>
          </>
        )}

        <ErrorBox msg={action.err} />
        <SuccessBox msg={action.out
          ? (() => {
              const parts = [];
              if (action.out.delivered) parts.push("Inbox notification delivered");
              if (action.out.email_sent) parts.push("HTML email sent");
              if (!parts.length) parts.push("Message saved — delivery will retry on next load");
              return `✓ ${parts.join(" · ")} to ${pickedUser?.email || "user"}.`;
            })()
          : ""} />
      </div>
    </AdminCard>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// G3 — Cost vs Practice scatter
// ─────────────────────────────────────────────────────────────────────────────
const QUADRANT_COLOUR = {
  high_cost_low_practice:  "#dc2626",
  high_cost_good_practice: "#f59e0b",
  low_cost_low_practice:   "#9333ea",
  healthy:                 "#10b981",
};
const QUADRANT_LABEL = {
  high_cost_low_practice:  "High cost + low practice",
  high_cost_good_practice: "High cost + good practice",
  low_cost_low_practice:   "Low cost + low practice",
  healthy:                 "Healthy",
};

function CostVsPracticeCard({ days }) {
  const { data, err } = useAdminFetch(`/coach/admin/cost-vs-practice?days=${days}&limit=200`, [days]);
  const [points, setPoints] = useState([]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const piiOn = await coachPiiEnabled();
      const decrypted = await Promise.all((data?.points || []).map(async p => ({
        ...p,
        email: await decryptPii(p.email, piiOn),
        name:  await decryptPii(p.name,  piiOn),
      })));
      if (!cancelled) setPoints(decrypted);
    })();
    return () => { cancelled = true; };
  }, [data]);

  const W = 560, H = 230, pad = 36;
  const { sx, sy } = useMemo(() => {
    const m = Math.max(1, ...points.map(p => p.cost_usd));
    return {
      sx: s => pad + (s / 100) * (W - 2 * pad),
      sy: c => H - pad - (c / m) * (H - 2 * pad),
    };
  }, [points]);

  return (
    <AdminCard title="Cost vs Practice"
               subtle={`Each dot is one user. Left = low score (bad habits), right = high score (good habits). Up = high spend, down = low spend. The red zone (top-left) is where expensive bad habits live · last ${days} days`} accent="slate">
      {!data && !err && <AdminSkeleton />}
      {err && <ErrorBox msg={err} />}
      {data && points.length === 0 && (
        <EmptyState text="Not enough data yet." />
      )}
      {data && points.length > 0 && (
        <>
          <svg width="100%" viewBox={`0 0 ${W} ${H}`}
               className="bg-slate-50/60 rounded-2xl border border-slate-200">
            <line x1={pad} y1={H-pad} x2={W-pad} y2={H-pad} stroke="#e2e8f0" strokeWidth="1.5" />
            <line x1={pad} y1={pad}   x2={pad}   y2={H-pad} stroke="#e2e8f0" strokeWidth="1.5" />
            <line x1={sx(60)} y1={pad} x2={sx(60)} y2={H-pad}
                  stroke="#cbd5e1" strokeDasharray="4 3" strokeWidth="1" />
            <text x={W-pad} y={H-pad+14} fontSize="9" textAnchor="end" fill="#94a3b8">score →</text>
            <text x={pad-4} y={pad-6}    fontSize="9" textAnchor="end" fill="#94a3b8">$ ↑</text>
            <text x={sx(60)} y={H-pad+14} fontSize="8" textAnchor="middle" fill="#94a3b8">60</text>
            {points.map(p => (
              <circle key={p.user_id}
                      cx={sx(p.score)} cy={sy(p.cost_usd)} r={5}
                      fill={QUADRANT_COLOUR[p.quadrant] || "#64748b"}
                      opacity="0.75"
                      className="hover:opacity-100 transition-opacity">
                <title>{`${p.email || p.user_id}\nScore: ${p.score} · $${p.cost_usd}\n${QUADRANT_LABEL[p.quadrant]}`}</title>
              </circle>
            ))}
          </svg>
          <div className="flex flex-wrap gap-3 mt-3">
            {Object.entries(QUADRANT_LABEL).map(([q, label]) => (
              <div key={q} className="flex items-center gap-1.5 text-[10.5px] text-slate-600">
                <span className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                      style={{ background: QUADRANT_COLOUR[q] }} />
                {label}
              </div>
            ))}
          </div>

        </>
      )}
    </AdminCard>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// E3 — Admin audit log
// ─────────────────────────────────────────────────────────────────────────────
const ACTION_CHIP = {
  "reset_score:soft": "bg-indigo-100 text-indigo-700",
  "reset_score:hard": "bg-red-100 text-red-700",
  "purge_events":     "bg-red-100 text-red-700",
  "disable_rule":     "bg-amber-100 text-amber-700",
  "enable_rule":      "bg-emerald-100 text-emerald-700",
  "manual_coach":     "bg-emerald-100 text-emerald-700",
};

function AuditCard({ days }) {
  const { data, err } = useAdminFetch(`/coach/admin/audit?days=${days}&limit=50`, [days]);
  const rows = data?.items || [];

  return (
    <AdminCard title="Admin Action History"
               subtle={`A record of every admin action taken in this panel — who reset a score, who deleted data, who silenced a rule. Visible to admins only · last ${days} days`} accent="slate">
      {!data && !err && <AdminSkeleton />}
      {err && <ErrorBox msg={err} />}
      {data && rows.length === 0 && (
        <EmptyState icon={ScrollText} text="No admin actions yet." />
      )}
      {rows.length > 0 && (
        <div className="bg-white border border-slate-200 rounded-2xl
                        shadow-[0_1px_2px_rgba(15,23,42,0.04)] overflow-hidden max-h-72">
          <div className="max-h-72 overflow-y-auto divide-y divide-slate-50">
            {rows.map(r => (
              <div key={r.id} className="flex items-start gap-3 px-3 py-2.5 hover:bg-slate-50/60 transition">
                <div className="flex-shrink-0 text-[10px] text-slate-400 whitespace-nowrap pt-0.5 font-mono">
                  {r.ts?.slice(0, 16).replace("T", " ")}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ${
                      ACTION_CHIP[r.action] || "bg-slate-100 text-slate-700"
                    }`}>
                      {r.action}
                    </span>
                    {r.target_user && (
                      <span className="text-[11px] text-slate-600 truncate max-w-[160px]">
                        {r.target_user}
                      </span>
                    )}
                    {r.rule_id && (
                      <code className="text-[10px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded font-mono">
                        {r.rule_id}
                      </code>
                    )}
                  </div>
                  <div className="text-[10px] text-slate-400 mt-0.5">
                    by {r.actor_email || r.actor_id}
                    {r.reason && <span className="ml-2 italic">— {r.reason}</span>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </AdminCard>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Weekly Mail — opt-out management + feature status
// ─────────────────────────────────────────────────────────────────────────────
function WeeklyMailCard() {
  const { data: status, err: statusErr, reload: reloadStatus } =
    useAdminFetch(`/coach/admin/weekly-mail/status`, []);
  const { data: optOutsRaw, err: optOutErr, reload: reloadOptOuts } =
    useAdminFetch(`/coach/admin/weekly-mail/opt-outs?limit=100`, []);

  const [optOuts, setOptOuts] = useState(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!optOutsRaw) { if (!cancelled) setOptOuts(optOutsRaw); return; }
      const piiOn = await coachPiiEnabled();
      const items = await Promise.all((optOutsRaw.items || []).map(async r => ({
        ...r,
        email: await decryptPii(r.email, piiOn),
        name:  await decryptPii(r.name,  piiOn),
      })));
      if (!cancelled) setOptOuts({ ...optOutsRaw, items });
    })();
    return () => { cancelled = true; };
  }, [optOutsRaw]);

  const [pickedUser, setPickedUser] = useState(null);
  const [reason,     setReason]     = useState("");

  const optOut = useAdminAction(async () => {
    if (!pickedUser) throw new Error("Please select a user first.");
    const d = await jsonOrThrow(await authFetch(`/coach/admin/weekly-mail/opt-out`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: pickedUser.email || pickedUser.id, reason }),
    }));
    reloadOptOuts(); reloadStatus(); setReason("");
    return d;
  });

  const optIn = useAdminAction(async (userId) => {
    const d = await jsonOrThrow(await authFetch(
      `/coach/admin/weekly-mail/opt-out/${encodeURIComponent(userId)}`,
      { method: "DELETE" }
    ));
    reloadOptOuts(); reloadStatus();
    return d;
  });

  const err = statusErr || optOutErr || optOut.err || optIn.err;

  return (
    <AdminCard
      title="Weekly Digest Email"
      subtle="Automated weekly HTML coaching digest sent to all users. Admins can opt specific users out. Controlled by COACH_WEEKLY_MAIL_ENABLED env var."
      accent="indigo"
    >
      <div className="space-y-4">

        {/* Feature status banner */}
        {status && (
          <div className={`flex items-center gap-2 px-3 py-2 rounded-xl text-[11px] font-medium border ${
            status.enabled
              ? "bg-emerald-50 border-emerald-200 text-emerald-800"
              : "bg-amber-50 border-amber-200 text-amber-800"
          }`}>
            <Mail className="w-3.5 h-3.5 flex-shrink-0" />
            {status.enabled
              ? `✓ Weekly digest is ENABLED — ${status.opt_out_count} user${status.opt_out_count !== 1 ? "s" : ""} opted out`
              : `⚠ Weekly digest is DISABLED — set COACH_WEEKLY_MAIL_ENABLED=true to activate`}
          </div>
        )}

        {/* User picker + actions */}
        <div>
          <FieldLabel>Select a user</FieldLabel>
          <UserPicker
            onChange={setPickedUser}
            placeholder="Search by name or email…"
          />
        </div>

        {/* Opt-out a user */}
        <div>
          <FieldLabel>Opt a user out of weekly digest</FieldLabel>
          <div className="flex gap-2">
            <input
              value={reason}
              onChange={e => setReason(e.target.value)}
              placeholder="Reason (optional)"
              className="flex-1 px-2.5 py-1.5 text-[11.5px] border border-slate-200 rounded-lg bg-white
                         focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300
                         placeholder:text-slate-400 transition shadow-sm"
            />
            <AdminBtn
              onClick={() => optOut.run()}
              busy={optOut.busy}
              busyLabel="Saving…"
              variant="amber"
            >
              <Mail className="w-3 h-3" /> Opt out
            </AdminBtn>
          </div>
          <SuccessBox msg={optOut.out
            ? optOut.out.already_opted_out
              ? `ℹ User was already opted out.`
              : `✓ User opted out of weekly digest.`
            : ""} />
        </div>

        <ErrorBox msg={err} />

        {/* Opted-out users list */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold">
              Currently opted out ({optOuts?.total ?? 0})
            </div>
            <button onClick={() => { reloadOptOuts(); reloadStatus(); }}
                    className="text-[11px] px-2 py-1 rounded-lg bg-slate-100 hover:bg-slate-200
                               text-slate-600 flex items-center gap-1 transition">
              <RefreshCw className="w-3 h-3" /> Refresh
            </button>
          </div>

          {!optOuts && !optOutErr && <AdminSkeleton />}
          {optOuts && (optOuts.items || []).length === 0 && (
            <EmptyState icon={CheckCircle2} text="No users opted out — everyone will receive the weekly digest." />
          )}
          {optOuts && (optOuts.items || []).length > 0 && (
            <div className="bg-white border border-slate-200 rounded-2xl overflow-hidden
                            shadow-[0_1px_2px_rgba(15,23,42,0.04)] max-h-44 overflow-y-auto">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-slate-400 uppercase text-[9.5px] tracking-widest font-semibold
                                 bg-slate-50/70 border-b border-slate-100">
                    <th className="px-3 py-2 text-left">User</th>
                    <th className="px-3 py-2 text-left">Opted out by</th>
                    <th className="px-3 py-2 text-left">Reason</th>
                    <th className="px-3 py-2 text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {(optOuts.items || []).map(r => (
                    <tr key={r.id}
                        className="border-b border-slate-50 last:border-0 hover:bg-slate-50/60 transition">
                      <td className="px-3 py-2">
                        <div className="font-medium text-slate-800 truncate max-w-[140px]">
                          {r.email || r.user_id}
                        </div>
                        {r.name && <div className="text-[10px] text-slate-400">{r.name}</div>}
                      </td>
                      <td className="px-3 py-2 text-slate-500 truncate max-w-[120px]">
                        {r.opted_out_by || "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-400 italic truncate max-w-[120px]">
                        {r.reason || "—"}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button
                          onClick={() => optIn.run(r.user_id)}
                          className="text-[10.5px] text-emerald-600 hover:text-emerald-700
                                     font-medium flex items-center gap-1 ml-auto transition"
                        >
                          <ShieldCheck className="w-3 h-3" /> Re-enable
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </AdminCard>
  );
}


// ─────────────────────────────────────────────────────────────────────────────
// FR-IMP-2 — Rule Playground
// Stateless REPL for the rule DSL. Backend gate: routers/coach_router.py
// already requires admin on POST /coach/rules/test, so a non-admin who guessed
// the URL would get 403 anyway — this card is the only admin entry point.
// ─────────────────────────────────────────────────────────────────────────────
const PLAYGROUND_DRAFT_DEFAULT = JSON.stringify({
  channel: "web",
  model: MODEL_DEFAULT,
  prompt: "fix it",
  tokens_in: 50,
  tokens_out: 100,
  latency_ms: 1500,
  context_window_pct: 0.35,
  tool_calls: [],
}, null, 2);

function PlaygroundCard() {
  const [draft,   setDraft]   = useState(PLAYGROUND_DRAFT_DEFAULT);
  const [result,  setResult]  = useState(null);
  const [err,     setErr]     = useState("");
  const [running, setRunning] = useState(false);

  const run = async () => {
    setErr(""); setRunning(true);
    let parsed;
    try { parsed = JSON.parse(draft); }
    catch (e) { setErr("Invalid JSON: " + e.message); setRunning(false); return; }
    try {
      const data = await jsonOrThrow(await authFetch(`/coach/rules/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event: parsed }),
      }));
      setResult(data);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <AdminCard title="Rule Playground"
               subtle="Stateless REPL for the baseline rules — no DB write. Mirrors CoachEvent fields."
               accent="indigo">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-stretch">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold mb-2">
            Synthetic Event (JSON)
          </div>
          <div className="relative">
            <textarea
              value={draft}
              onChange={e => setDraft(e.target.value)}
              className="w-full h-64 font-mono text-[11px] border border-slate-200 rounded-lg p-3 bg-slate-50/60 text-slate-800 focus:outline-none focus:border-indigo-300 focus:bg-white transition"
              spellCheck={false}
            />
            <div className="absolute top-2 right-2 text-[9px] uppercase tracking-widest text-slate-300 font-bold pointer-events-none">JSON</div>
          </div>
          <button onClick={run} disabled={running}
                  className="group mt-3 px-3 py-2 text-[11.5px] font-medium brand-grad-r text-white rounded-lg hover:shadow-md hover:shadow-indigo-200 disabled:opacity-60 disabled:cursor-not-allowed cursor-pointer flex items-center gap-1.5 transition">
            <FlaskConical size={12} className="group-hover:rotate-12 transition-transform" />
            {running ? "Evaluating…" : "Run all baseline rules"}
          </button>
          {err && (
            <div className="text-[11px] text-red-600 mt-2 px-2 py-1 bg-red-50 border border-red-100 rounded-md">{err}</div>
          )}
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold mb-2">
            Hits
          </div>
          {!result && (
            <div className="text-[11.5px] text-slate-400 italic">Run the rules to see hits.</div>
          )}
          {result && result.hits.length === 0 && (
            <div className="flex items-center gap-2 text-[11.5px] text-emerald-700 px-2 py-1.5 bg-emerald-50 border border-emerald-100 rounded-lg">
              <CheckCircle size={13} /> No rules fired. Clean event.
            </div>
          )}
          {result && result.hits.length > 0 && (
            <ul className="space-y-1.5">
              {result.hits.map(h => (
                <li key={h.id}
                    className="flex items-center gap-2 text-[11.5px] py-1.5 px-2 bg-white border border-slate-200 rounded-lg">
                  <span className={`text-[9px] px-1.5 py-0.5 rounded-full font-bold uppercase tracking-wider ${SEVERITY_STYLE[h.severity] || "bg-slate-100 text-slate-700"}`}>
                    {h.severity}
                  </span>
                  <code className="font-mono font-semibold text-slate-700">{h.id}</code>
                  <span className="text-slate-500 truncate">{h.name}</span>
                </li>
              ))}
            </ul>
          )}
          {result && (
            <div className="text-[10px] text-slate-400 mt-3 italic">
              Evaluated {result.evaluated.length} baseline rule{result.evaluated.length === 1 ? "" : "s"}.
            </div>
          )}
        </div>
      </div>
    </AdminCard>
  );
}


// ─────────────────────────────────────────────────────────────────────────────
// Department Breakdown — replaces the old "Org Rollups" tab.
// Admins can filter to a single department; shows events, rule hits by
// category, and hits by severity — same data, now with a dept drill-down.
// ─────────────────────────────────────────────────────────────────────────────
const CATEGORY_LABEL = {
  "prompt-quality":     "Prompt Quality",
  "session-hygiene":    "Session Hygiene",
  "review-discipline":  "Review Discipline",
  "tool-mastery":       "Tool Mastery",
  "context-management": "Context Management",
  "security":           "Security",
};

const SEVERITY_STYLE = {
  low:      "bg-slate-100 text-slate-700",
  medium:   "bg-amber-100 text-amber-700",
  high:     "bg-orange-100 text-orange-800",
  critical: "bg-red-100 text-red-800",
};

function DeptBreakdownSection({ days }) {
  const [applied, setApplied] = useState("");  // exact dept name being filtered

  // Load distinct departments from real coach_event data (last 90 days)
  const { data: deptData, reload: reloadDepts } = useAdminFetch(`/coach/admin/departments?days=90`, []);
  const departments = deptData?.departments || [];

  // Searchable dropdown state
  const [query,   setQuery]   = useState("");
  const [open,    setOpen]    = useState(false);
  const dropRef = useRef(null);

  // Close on outside click
  useEffect(() => {
    const h = e => { if (dropRef.current && !dropRef.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const filtered = departments.filter(d =>
    !query || d.toLowerCase().includes(query.toLowerCase())
  );

  const select = d => {
    setApplied(d);
    setQuery(d);
    setOpen(false);
  };

  const clear = () => {
    setApplied("");
    setQuery("");
    setOpen(false);
  };

  const qs = applied ? `?days=${days}&department=${encodeURIComponent(applied)}` : `?days=${days}`;
  const { data, err, reload } = useAdminFetch(`/coach/org/rollup${qs}`, [days, applied]);

  const renderBars = (rows, labelKey, labelFn) => {
    const max = rows.length ? Math.max(...rows.map(r => r.count)) : 0;
    return (
      <ul className="space-y-2">
        {rows.map(r => {
          const pct = max ? (r.count / max) * 100 : 0;
          return (
            <li key={r[labelKey]} className="text-[11.5px]">
              <div className="flex items-center justify-between mb-1">
                <span className="text-slate-700 font-medium">{labelFn(r)}</span>
                <span className="text-slate-500 font-mono">{r.count}</span>
              </div>
              <div className="h-1.5 bg-slate-100 rounded-full overflow-hidden">
                <div className="h-full brand-grad-r transition-all duration-500"
                     style={{ width: `${Math.max(4, pct)}%` }} />
              </div>
            </li>
          );
        })}
      </ul>
    );
  };

  return (
    <div>
      {/* Section heading */}
      <div className="flex items-center gap-2 mb-3">
        <Building2 className="w-4 h-4 text-slate-400" />
        <span className="text-[12px] font-bold text-slate-800">Department Breakdown</span>
        <span className="text-[10.5px] text-slate-400">
          — usage and rule violations across the org
        </span>
      </div>

      {/* Department filter bar — searchable dropdown */}
      <div className="flex items-center gap-2 mb-4 flex-wrap">

        {/* Dropdown */}
        <div ref={dropRef} className="relative w-72">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
            <input
              value={query}
              onChange={e => { setQuery(e.target.value); setApplied(""); setOpen(true); }}
              onFocus={() => setOpen(true)}
              placeholder={departments.length ? `All departments (${departments.length} available)` : "Loading departments…"}
              className="w-full pl-8 pr-8 py-1.5 text-[11.5px] border border-slate-200 rounded-lg bg-white
                         focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300
                         placeholder:text-slate-400 transition shadow-sm"
            />
            {query && (
              <button onClick={clear}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 transition">
                <X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>

          {/* Applied chip below input */}
          {applied && (
            <div className="mt-1.5 flex items-center gap-1.5 px-2.5 py-1.5 bg-indigo-50
                            border border-indigo-200 rounded-lg text-[11px] text-indigo-800">
              <Building2 className="w-3 h-3 flex-shrink-0 text-indigo-400" />
              <span className="font-medium truncate">{applied}</span>
              <button onClick={clear} className="ml-auto text-indigo-400 hover:text-indigo-600 transition flex-shrink-0">
                <X className="w-3 h-3" />
              </button>
            </div>
          )}

          {/* Dropdown list */}
          {open && (
            <div className="absolute z-50 mt-1 w-full bg-white border border-slate-200 rounded-xl
                            shadow-[0_4px_16px_rgba(15,23,42,0.10)] overflow-hidden max-h-56 overflow-y-auto">
              {/* All departments option */}
              <button
                onClick={clear}
                className={`w-full text-left px-3 py-2 text-[11.5px] border-b border-slate-50
                            hover:bg-slate-50 transition flex items-center gap-2
                            ${!applied ? "bg-indigo-50 text-indigo-700 font-medium" : "text-slate-500"}`}
              >
                <span className="text-slate-400">—</span> All departments
              </button>

              {filtered.length === 0 && (
                <div className="px-3 py-2 text-[11px] text-slate-400 italic">
                  {departments.length === 0 ? "No departments found in the last 90 days." : "No match."}
                </div>
              )}

              {filtered.map(d => (
                <button
                  key={d}
                  onClick={() => select(d)}
                  className={`w-full text-left px-3 py-2 text-[11.5px] border-b border-slate-50
                              last:border-0 hover:bg-indigo-50/60 transition flex items-center gap-2
                              ${applied === d ? "bg-indigo-50 text-indigo-700 font-semibold" : "text-slate-700"}`}
                >
                  <Building2 className="w-3 h-3 text-slate-300 flex-shrink-0" />
                  {d}
                  {applied === d && <span className="ml-auto text-indigo-400 text-[10px]">✓</span>}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Refresh */}
        <button onClick={() => { reload(); reloadDepts(); }}
                className="ml-auto text-[11px] px-2 py-1 rounded-lg bg-slate-100 hover:bg-slate-200
                           text-slate-600 flex items-center gap-1 transition">
          <RefreshCw className="w-3 h-3" /> Refresh
        </button>
      </div>

      {/* Cards */}
      {!data && !err && <AdminSkeleton />}
      {err && <ErrorBox msg={err} />}
      {data && (
        <div className="space-y-4">
          {/* Events by department — full width */}
          <AdminCard
            title="Events by Department"
            subtle="How many AI interactions each department had in this window"
            accent="indigo"
          >
            {(data.events_by_department || []).length
              ? renderBars(data.events_by_department, "department", r => r.department || "Unknown")
              : <div className="text-[11px] text-slate-400 italic py-2">No events in this window.</div>}
          </AdminCard>

          {/* Hits by category + hits by severity — side by side */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-stretch">
            <AdminCard
              title="Rule Violations by Category"
              subtle="Which coaching categories are firing most — helps identify where training is needed"
              accent="amber"
            >
              {(data.hits_by_category || []).length
                ? renderBars(
                    data.hits_by_category, "category",
                    r => CATEGORY_LABEL[r.category] || r.category
                  )
                : <div className="text-[11px] text-slate-400 italic py-2">No violations in this window.</div>}
            </AdminCard>

            <AdminCard
              title="Violations by Severity"
              subtle="How serious the violations are — critical and high need the most attention"
              accent="red"
            >
              {(data.hits_by_severity || []).length ? (
                <ul className="space-y-2">
                  {data.hits_by_severity.map(h => (
                    <li key={h.severity}
                        className="flex items-center justify-between text-[11.5px] py-1.5
                                   px-2 -mx-1 rounded-lg hover:bg-slate-50 transition">
                      <span className={`text-[9px] px-2 py-0.5 rounded-full font-bold uppercase
                                        tracking-wider ${SEVERITY_STYLE[h.severity] || "bg-slate-100 text-slate-700"}`}>
                        {h.severity}
                      </span>
                      <span className="text-slate-700 font-mono font-semibold">{h.count}</span>
                    </li>
                  ))}
                </ul>
              ) : <div className="text-[11px] text-slate-400 italic py-2">No violations in this window.</div>}
            </AdminCard>
          </div>
        </div>
      )}
    </div>
  );
}
