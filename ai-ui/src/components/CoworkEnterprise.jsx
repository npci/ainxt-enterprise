// SPDX-License-Identifier: Apache-2.0
// CoworkEnterprise — admin-only enterprise controls for Buddy:
//   • Connector policy: per-tool allow/deny by org/department
//   • Spend limits: per-department monthly USD cap
//   • Usage analytics: month-to-date cost/tokens/turns by department + top users
// Backed by routers/cowork_policy_router.py + cowork_usage_router.py. Read-only for
// non-admins (component is only mounted when isAdmin).
import { useEffect, useState, useCallback } from "react";
import { ShieldCheck, Plus, Trash2, Gauge, BarChart3, Loader2 } from "lucide-react";
import { API_BASE as API, authFetch } from "../config";

export default function CoworkEnterprise() {
  const [rules, setRules] = useState([]);
  const [connectors, setConnectors] = useState([]);
  const [limits, setLimits] = useState([]);
  const [analytics, setAnalytics] = useState(null);
  const [busy, setBusy] = useState(false);
  // new-rule form
  const [nr, setNr] = useState({ department: "", connector: "", tool: "*", allow: false });
  const [nl, setNl] = useState({ department: "", monthly_usd: 0 });

  const load = useCallback(async () => {
    try {
      const [p, c, l, a] = await Promise.all([
        authFetch(`${API}/buddy/connector-policy`).then((x) => x.json()).catch(() => ({})),
        authFetch(`${API}/connectors/available`).then((x) => x.json()).catch(() => []),
        authFetch(`${API}/buddy/spend-limits`).then((x) => x.json()).catch(() => ({})),
        authFetch(`${API}/buddy/usage/analytics`).then((x) => x.json()).catch(() => null),
      ]);
      setRules(p?.rules || []);
      setConnectors(Array.isArray(c) ? c : (c?.connectors || []));
      setLimits(l?.limits || []);
      setAnalytics(a);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const addRule = async () => {
    if (!nr.connector.trim()) return;
    setBusy(true);
    try {
      await authFetch(`${API}/buddy/connector-policy`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(nr),
      });
      setNr({ department: "", connector: "", tool: "*", allow: false });
      await load();
    } finally { setBusy(false); }
  };
  const delRule = async (id) => { await authFetch(`${API}/buddy/connector-policy/${id}`, { method: "DELETE" }); load(); };

  const saveLimit = async () => {
    if (!nl.department.trim()) return;
    setBusy(true);
    try {
      await authFetch(`${API}/buddy/spend-limits`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ department: nl.department.trim(), monthly_usd: Number(nl.monthly_usd) || 0 }),
      });
      setNl({ department: "", monthly_usd: 0 });
      await load();
    } finally { setBusy(false); }
  };

  const usd = (n) => `$${(Number(n) || 0).toFixed(2)}`;

  return (
    <div className="mt-10 space-y-10">
      {/* ── Connector policy ─────────────────────────────────────────────── */}
      <section>
        <div className="flex items-center gap-2 mb-1">
          <ShieldCheck className="w-4 h-4 text-indigo-600" />
          <h2 className="text-sm font-semibold text-gray-700">Connector policy</h2>
        </div>
        <p className="text-xs text-gray-500 mb-3 max-w-2xl">
          Allow or <b>deny</b> a connector (or one tool) for the whole organization or a department.
          Deny wins. A blank department = org-wide; <code>*</code> = the whole connector. Applies to
          every Buddy agent (desktop + server).
        </p>
        <div className="flex flex-wrap items-end gap-2 mb-3">
          <div>
            <label className="block text-[11px] text-gray-500 mb-0.5">Department</label>
            <input value={nr.department} onChange={(e) => setNr({ ...nr, department: e.target.value })}
              placeholder="(org-wide)" className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm w-36 outline-none" />
          </div>
          <div>
            <label className="block text-[11px] text-gray-500 mb-0.5">Connector</label>
            <input list="cowork-conn-list" value={nr.connector} onChange={(e) => setNr({ ...nr, connector: e.target.value })}
              placeholder="e.g. microsoft_365" className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm w-44 outline-none" />
            <datalist id="cowork-conn-list">
              {connectors.map((c) => <option key={c.name} value={c.name} />)}
            </datalist>
          </div>
          <div>
            <label className="block text-[11px] text-gray-500 mb-0.5">Tool</label>
            <input value={nr.tool} onChange={(e) => setNr({ ...nr, tool: e.target.value })}
              placeholder="*" className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm w-40 outline-none" />
          </div>
          <div>
            <label className="block text-[11px] text-gray-500 mb-0.5">Decision</label>
            <select value={nr.allow ? "allow" : "deny"} onChange={(e) => setNr({ ...nr, allow: e.target.value === "allow" })}
              className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm bg-white outline-none">
              <option value="deny">Deny</option>
              <option value="allow">Allow</option>
            </select>
          </div>
          <button onClick={addRule} disabled={busy || !nr.connector.trim()}
            className="flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40">
            <Plus className="w-4 h-4" /> Add rule
          </button>
        </div>
        <div className="border border-gray-200 rounded-lg divide-y divide-gray-100">
          {rules.length === 0 && <div className="px-3 py-3 text-xs text-gray-400">No rules — all connectors allowed by default.</div>}
          {rules.map((r) => (
            <div key={r.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${r.allow ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}>
                {r.allow ? "ALLOW" : "DENY"}
              </span>
              <span className="font-mono text-gray-800">{r.connector}<span className="text-gray-400">__{r.tool}</span></span>
              <span className="text-xs text-gray-500">{r.department || "org-wide"}</span>
              <button onClick={() => delRule(r.id)} className="ml-auto p-1 text-gray-400 hover:text-red-600"><Trash2 className="w-4 h-4" /></button>
            </div>
          ))}
        </div>
      </section>

      {/* ── Spend limits ─────────────────────────────────────────────────── */}
      <section>
        <div className="flex items-center gap-2 mb-1">
          <Gauge className="w-4 h-4 text-indigo-600" />
          <h2 className="text-sm font-semibold text-gray-700">Department spend limits</h2>
        </div>
        <p className="text-xs text-gray-500 mb-3 max-w-2xl">Monthly USD cap per department (0 = unlimited). Buddy warns/blocks new runs once a department is over.</p>
        <div className="flex items-end gap-2 mb-3">
          <div>
            <label className="block text-[11px] text-gray-500 mb-0.5">Department</label>
            <input value={nl.department} onChange={(e) => setNl({ ...nl, department: e.target.value })}
              placeholder="e.g. FINANCE" className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm w-44 outline-none" />
          </div>
          <div>
            <label className="block text-[11px] text-gray-500 mb-0.5">Monthly USD</label>
            <input type="number" min="0" value={nl.monthly_usd} onChange={(e) => setNl({ ...nl, monthly_usd: e.target.value })}
              className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm w-32 outline-none" />
          </div>
          <button onClick={saveLimit} disabled={busy || !nl.department.trim()}
            className="text-sm px-3 py-1.5 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40">Set limit</button>
        </div>
        <div className="border border-gray-200 rounded-lg divide-y divide-gray-100">
          {limits.length === 0 && <div className="px-3 py-3 text-xs text-gray-400">No limits set — all departments unlimited.</div>}
          {limits.map((l) => (
            <div key={l.department} className="flex items-center gap-3 px-3 py-2 text-sm">
              <span className="font-medium text-gray-800">{l.department || "(org-wide)"}</span>
              <span className="text-gray-500">{l.monthly_usd > 0 ? `${usd(l.monthly_usd)}/mo` : "unlimited"}</span>
            </div>
          ))}
        </div>
      </section>

      {/* ── Usage analytics ──────────────────────────────────────────────── */}
      <section>
        <div className="flex items-center gap-2 mb-1">
          <BarChart3 className="w-4 h-4 text-indigo-600" />
          <h2 className="text-sm font-semibold text-gray-700">Usage analytics <span className="text-xs text-gray-400 font-normal">— this month</span></h2>
        </div>
        {!analytics ? (
          <div className="flex items-center gap-2 text-sm text-gray-400 py-4"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
        ) : (
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <div className="text-xs font-medium text-gray-600 mb-1">By department</div>
              <div className="border border-gray-200 rounded-lg divide-y divide-gray-100 text-sm">
                {(analytics.by_department || []).length === 0 && <div className="px-3 py-3 text-xs text-gray-400">No usage yet.</div>}
                {(analytics.by_department || []).map((d) => (
                  <div key={d.department} className="flex items-center gap-2 px-3 py-1.5">
                    <span className="font-medium text-gray-800 flex-1 truncate">{d.department}</span>
                    <span className="text-gray-500">{usd(d.cost_usd)}</span>
                    <span className="text-[11px] text-gray-400">{d.users}u · {d.turns}t</span>
                  </div>
                ))}
              </div>
            </div>
            <div>
              <div className="text-xs font-medium text-gray-600 mb-1">Top users</div>
              <div className="border border-gray-200 rounded-lg divide-y divide-gray-100 text-sm max-h-64 overflow-y-auto">
                {(analytics.top_users || []).length === 0 && <div className="px-3 py-3 text-xs text-gray-400">No usage yet.</div>}
                {(analytics.top_users || []).map((u) => (
                  <div key={u.user_id} className="flex items-center gap-2 px-3 py-1.5">
                    <span className="font-mono text-[11px] text-gray-700 flex-1 truncate" title={u.user_id}>{u.user_id}</span>
                    <span className="text-gray-500">{usd(u.cost_usd)}</span>
                    <span className="text-[11px] text-gray-400">{u.turns}t</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
