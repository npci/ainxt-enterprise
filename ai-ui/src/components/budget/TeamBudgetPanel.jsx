// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useMemo, Fragment } from "react";
import { Users, ChevronDown, ChevronRight, Clock, ArrowUpDown } from "lucide-react";

import { API_BASE as API, apiFetch, authFetch } from "../../config";
import { UserBudgetDetail } from "../BudgetManager";
import { decryptPii } from "../../utils/piiCrypto";
import { SORT_DESC, sortByUtilisation, toggleSortDirection } from "./utilisationSort";
import { UtilizationPage } from "./UtilizationView";
import { utilizationEndpoints } from "./utilizationEndpoints";

// PII payload encryption flag (core/pii_crypto.py), fetched once per page load
// from the unauthenticated /auth/ui-config endpoint and shared by every render.
// GET /budget/team returns `email` and `display_name` wrapped by encrypt_pii(),
// so without this the table printed raw "pii:v1:..." tokens where names and
// email addresses belong — and "Search team..." silently matched nothing,
// because it filters on those same fields.
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

// Decrypt the PII fields of a /budget/team response in place of the raw payload.
// Kept in one helper so the initial load and the Retry path cannot drift apart —
// only one of them decrypting is exactly the kind of asymmetry that caused this.
async function decryptTeamPayload(d) {
  const on = await piiEnabled();
  if (!on) return d;
  const reports = await Promise.all(
    (d?.reports || []).map(async (r) => ({
      ...r,
      email:        await decryptPii(r.email,        on),
      display_name: await decryptPii(r.display_name, on),
    })),
  );
  return {
    ...d,
    reports,
    caller: d?.caller
      ? {
          ...d.caller,
          email:        await decryptPii(d.caller.email,        on),
          display_name: await decryptPii(d.caller.display_name, on),
        }
      : d?.caller,
  };
}

// Small clickable stat box for the team-aggregate summary row.
function TeamStatBox({ label, value, sub, tone = "neutral", onClick }) {
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
    </div>
  );
}

// ── Reporting-manager team view (read-only) ────────────────────────────────
// Mirrors the admin UserRosterPanel layout: a single table with base/extra/
// total/utilisation columns and an expandable drill-down that reuses
// UserBudgetDetail (same StatCards + history + increase audit as My Budget).
const PAGE_SIZE = 50;

export default function TeamBudgetPanel() {
  const [team,         setTeam]         = useState(null);
  const [loading,      setLoading]      = useState(true);
  const [error,        setError]        = useState("");
  const [search,       setSearch]       = useState("");
  const [selected,     setSelected]     = useState(null);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [sortDir,      setSortDir]      = useState(SORT_DESC);   // highest utilisation first
  const [teamDrill,    setTeamDrill]    = useState(false);       // team-wide utilization sub-page

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError("");
      try {
        const r = await authFetch(`${API}/budget/team`);
        if (cancelled) return;
        if (!r.ok) { setError("Failed to load team data"); return; }
        const d = await decryptTeamPayload(await r.json());
        if (!cancelled) setTeam(d);
      } catch {
        if (!cancelled) setError("Failed to load team data");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  async function loadTeam() {
    setLoading(true);
    setError("");
    try {
      const r = await authFetch(`${API}/budget/team`);
      if (!r.ok) { setError("Failed to load team data"); return; }
      setTeam(await decryptTeamPayload(await r.json()));
    } catch {
      setError("Failed to load team data");
    } finally {
      setLoading(false);
    }
  }

  const reports = team?.reports || [];

  // Filter by search, then order by utilisation (absolute cost spent).
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = !q ? reports : reports.filter(r =>
      (r.display_name || "").toLowerCase().includes(q) ||
      (r.email         || "").toLowerCase().includes(q) ||
      (r.title         || "").toLowerCase().includes(q) ||
      (r.department    || "").toLowerCase().includes(q)
    );
    return sortByUtilisation(matched, sortDir);
  }, [reports, search, sortDir]);

  // Team-wide aggregate totals across all reports (not filtered by search).
  const agg = useMemo(() => {
    return reports.reduce((acc, r) => {
      const base  = Number(r.base_cost_usd  ?? 0);
      const extra = Number(r.extra_cost_usd ?? 0);
      const total = Number(r.max_cost_usd_total ?? base + extra) || (base + extra);
      const spent = Number(r.usage_total?.cost_usd_spent ?? 0);
      acc.base  += base;
      acc.extra += extra;
      acc.total += total;
      acc.spent += spent;
      return acc;
    }, { base: 0, extra: 0, total: 0, spent: 0 });
  }, [reports]);
  const aggPct = agg.total > 0 ? Math.min(100, Math.round((agg.spent / agg.total) * 100)) : 0;

  function onSearchChange(v) {
    setSearch(v);
    setVisibleCount(PAGE_SIZE);
  }

  function toggleSort() {
    setSortDir(d => toggleSortDirection(d));
    setVisibleCount(PAGE_SIZE);
  }

  const visible = filtered.slice(0, visibleCount);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center flex-1 text-gray-400">
        <Clock size={28} className="mb-2 animate-pulse" />
        <p className="text-sm">Loading team budgets...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center flex-1 text-gray-400">
        <p className="text-sm text-red-500">{error}</p>
        <button onClick={loadTeam} className="mt-2 text-xs text-indigo-600 hover:text-indigo-800 underline cursor-pointer">
          Retry
        </button>
      </div>
    );
  }

  if (!team?.is_team_viewer || !reports.length) {
    return (
      <div className="flex flex-col items-center justify-center flex-1 text-gray-300">
        <Users size={48} />
        <p className="mt-3 text-sm">
          {team?.is_team_viewer === false
            ? "You don't have any reports configured in the org directory."
            : "None of your reports have AiNxt accounts yet."}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col flex-1 overflow-hidden">
      {/* Truncation warning — hidden while viewing the utilization drill */}
      {!teamDrill && team.truncated && (
        <div className="border-b border-amber-200 px-4 py-2 bg-amber-50 text-xs text-amber-700">
          Showing first 1,000 team members. Some reports may not be visible.
        </div>
      )}

      {/* Info bar + search — hidden while viewing the utilization drill */}
      {!teamDrill && (
        <div className="border-b border-gray-200 px-4 py-2.5 bg-gray-50/60 flex items-center justify-between gap-3">
          <span className="text-xs text-gray-500">
            Showing <strong className="text-gray-700">{team.total_count}</strong> team member{team.total_count !== 1 ? "s" : ""}
          </span>
          <input
            value={search}
            onChange={e => onSearchChange(e.target.value)}
            placeholder="Search team..."
            className="w-full max-w-xs px-3 py-1.5 text-sm border border-gray-200 rounded-md outline-none focus:border-indigo-300 shadow-sm bg-white"
          />
        </div>
      )}

      {/* Roster table — same layout as UserRosterPanel */}
      <div className="flex-1 overflow-y-auto p-6 max-w-6xl mx-auto w-full">
        {!teamDrill && (
          <div className="mb-3 flex items-start gap-3 p-3 border border-gray-200 bg-gray-50 rounded-lg">
            <Users size={16} className="text-gray-500 flex-shrink-0 mt-0.5" />
            <div className="text-xs text-gray-700">
              Read-only team view. You can see budget and usage for your direct and indirect reports.
              Everyone shares the same $50 base; extra budget comes from approved HOD increase
              requests and from 10x Winner grants, which carry over month to month until spent.
            </div>
          </div>
        )}

        {teamDrill ? (
          <UtilizationPage
            onBack={() => setTeamDrill(false)}
            endpoint={utilizationEndpoints.team()}
            options={[
              { value: "channel", label: "Channel wise usage" },
              { value: "model",   label: "Model wise usage" },
            ]}
            defaultView="channel"
          />
        ) : (
        <>
        {/* Team-wide aggregate summary */}
        <div className="mb-4">
          <div className="text-[11px] uppercase tracking-wide text-gray-500 font-medium mb-2">Team totals</div>
          <div className="flex flex-wrap gap-3">
            <TeamStatBox
              label="Allocated (base)"
              value={`$${agg.base.toFixed(2)}`}
              sub={`${reports.length} member${reports.length !== 1 ? "s" : ""}`}
            />
            <TeamStatBox
              label="Extra budget"
              value={`$${agg.extra.toFixed(2)}`}
              sub={agg.extra > 0 ? "Approved on top of base" : "None yet"}
              tone={agg.extra > 0 ? "green" : "neutral"}
            />
            <TeamStatBox
              label="Utilisation"
              value={`${aggPct}%`}
              sub={`$${agg.spent.toFixed(2)} / $${agg.total.toFixed(2)}`}
              tone={aggPct >= 90 ? "red" : aggPct >= 70 ? "yellow" : "green"}
              onClick={() => setTeamDrill(true)}
            />
          </div>
        </div>

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
                    No team members match your search.
                  </td>
                </tr>
              ) : visible.map(member => {
                const base  = Number(member.base_cost_usd  ?? 0);
                const extra = Number(member.extra_cost_usd ?? 0);
                const total = Number(member.max_cost_usd_total ?? base + extra) || (base + extra);
                const spent = Number(member.usage_total?.cost_usd_spent ?? 0);
                const pct   = total > 0 ? Math.min(100, Math.round((spent / total) * 100)) : 0;
                const color = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-yellow-400" : "bg-green-500";
                const isOpen = selected?.user_id === member.user_id;
                return (
                  <Fragment key={member.user_id || member.email}>
                    <tr className={`border-t border-gray-100 align-top ${isOpen ? "bg-indigo-50/40" : ""}`}>
                      <td className="px-4 py-3">
                        <div className="text-gray-800 font-medium truncate max-w-[22ch]">
                          {member.display_name || member.email || member.user_id}
                        </div>
                        <div className="text-xs text-gray-400 truncate max-w-[24ch]">{member.email}</div>
                        {(member.title || member.department) && (
                          <div className="text-[10px] text-gray-400 mt-0.5 truncate max-w-[28ch]">
                            {[member.title, member.department].filter(Boolean).join(" · ")}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right text-gray-700">${base.toFixed(2)}</td>
                      <td className="px-4 py-3 text-right text-gray-700">${extra.toFixed(2)}</td>
                      <td className="px-4 py-3 text-right text-gray-800 font-medium">${total.toFixed(2)}</td>
                      <td className="px-4 py-3 min-w-[12rem]">
                        {total > 0 ? (
                          <>
                            <div className="text-xs text-gray-600 mb-1">${spent.toFixed(2)} / ${total.toFixed(2)} ({pct}%)</div>
                            <div className="h-1.5 bg-gray-200 rounded-full">
                              <div className={`h-1.5 rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
                            </div>
                          </>
                        ) : (
                          <span className="text-xs text-gray-400 italic">No budget allocated</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => setSelected(isOpen ? null : member)}
                          className="cursor-pointer inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-gray-600 border border-gray-200 hover:bg-gray-50"
                        >
                          {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                          Details
                        </button>
                      </td>
                    </tr>
                    {isOpen && member.user_id && (
                      <tr className="bg-gray-50/60">
                        <td colSpan={6} className="px-4 py-3">
                          <UserBudgetDetail userId={member.user_id} userName={member.display_name || member.email} />
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
        </>
        )}
      </div>
    </div>
  );
}
