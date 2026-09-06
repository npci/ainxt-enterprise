// SPDX-License-Identifier: MIT
import { useState, useEffect, Fragment } from "react";
import { usePermission } from "../hooks/usePermission";
import { API_BASE } from "../config";
import { toISTDate } from "../utils/time";

// Self-improving skill loop — admin/approver audit + HITL approval surface.
// Lists auto-synthesized skill PROPOSALS captured from repeated successful runs.
// A PROPOSED→SKILL_CREATED proposal produces a PENDING_APPROVAL skill; approvers
// approve→promote (it becomes PRODUCTION) or reject it via the governance API.

const PROPOSAL_STATUS_COLORS = {
  PROPOSED:             "bg-yellow-100 text-yellow-700",
  SKILL_CREATED:        "bg-blue-100 text-blue-700",
  DISCARDED_COMPLIANCE: "bg-red-100 text-red-700",
  DISCARDED_DUP:        "bg-gray-200 text-gray-500",
  REJECTED:             "bg-red-100 text-red-700",
};

const SKILL_STATUS_COLORS = {
  PENDING_APPROVAL: "bg-yellow-100 text-yellow-700",
  APPROVED:         "bg-blue-100 text-blue-700",
  PRODUCTION:       "bg-green-100 text-green-700",
  REJECTED:         "bg-red-100 text-red-700",
  DEPRECATED:       "bg-gray-200 text-gray-500",
};

const FILTERS = ["ALL", "SKILL_CREATED", "PROPOSED", "DISCARDED_COMPLIANCE", "DISCARDED_DUP"];

const HEADERS = { "Content-Type": "application/json" };

export default function SkillProposals({ user }) {
  const { canApprove } = usePermission(user);
  const [items,        setItems]        = useState([]);
  const [filterStatus, setFilter]       = useState("ALL");
  const [loading,      setLoading]      = useState(false);
  const [busy,         setBusy]         = useState("");      // skill_name currently acting on
  const [expanded,     setExpanded]     = useState(null);    // proposal id
  const [rejectModal,  setRejectModal]  = useState(null);    // { skill_name }
  const [rejectReason, setRejectReason] = useState("");

  const load = () => {
    setLoading(true);
    const proposalsUrl = `${API_BASE}/skills/proposals`;
    fetch(proposalsUrl, { headers: HEADERS, credentials: "include" })
      .then(proposalsRes => { if (!proposalsRes.ok) throw new Error(proposalsRes.status); return proposalsRes.json(); })
      .then(proposalsData => {
        const raw = Array.isArray(proposalsData) ? proposalsData : (proposalsData.proposals || []);
        setItems(raw.map(item => ({
          ...item,
          skill_name: String(item.skill_name || '').replace(/[^a-zA-Z0-9_\-]/g, ''),
        })));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  // Governance actions act on the produced skill (by skill_name), not the proposal.
  const govAction = async (skillName, verb, body = {}) => {
    const safeSkillName = String(skillName).replace(/[^a-zA-Z0-9_\-]/g, '');
    const safeVerb = String(verb).replace(/[^a-zA-Z0-9_\-]/g, '');
    if (!safeSkillName || !safeVerb) return;
    const govActionUrl = `${API_BASE}/governance/skills/${safeSkillName}/${safeVerb}`;
    await fetch(govActionUrl, {
      method: "POST", headers: HEADERS, credentials: "include", body: JSON.stringify(body),
    });
  };

  const approveAndPromote = async (skillName) => {
    setBusy(skillName);
    try {
      await govAction(skillName, "approve");
      await govAction(skillName, "promote");
    } finally {
      setBusy("");
      load();
    }
  };

  const promote = async (skillName) => {
    setBusy(skillName);
    try { await govAction(skillName, "promote"); }
    finally { setBusy(""); load(); }
  };

  const reject = async (skillName, reason) => {
    setBusy(skillName);
    try { await govAction(skillName, "reject", { reason }); }
    finally { setBusy(""); load(); }
  };

  const visible = filterStatus === "ALL"
    ? items
    : items.filter(i => i.status === filterStatus);

  const pendingCount = items.filter(i => i.skill_status === "PENDING_APPROVAL").length;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-xl font-semibold text-gray-800 mb-1">Skill Proposals</h1>
      <p className="text-xs text-gray-400 mb-4">
        Skills auto-proposed by the self-improving loop from repeated successful runs.
        {pendingCount > 0 && (
          <span className="ml-1 text-yellow-600 font-medium">
            {pendingCount} awaiting approval
          </span>
        )}
      </p>

      {/* Status filters */}
      <div className="flex gap-2 mb-3 flex-wrap">
        {FILTERS.map(s => (
          <button key={s} onClick={() => setFilter(s)}
            className={`px-2 py-1 rounded-full text-xs transition ${
              filterStatus === s ? "bg-gray-700 text-white" : "bg-gray-100 text-gray-500 hover:bg-gray-200"
            }`}>{s.replace(/_/g," ")}</button>
        ))}
        <div className="flex-1" />
        <button onClick={load}
          className="px-2 py-1 rounded-full text-xs bg-gray-100 text-gray-500 hover:bg-gray-200 transition">
          Refresh
        </button>
      </div>

      {/* Table */}
      <div className="border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
            <tr>
              <th className="px-4 py-3 text-left">Proposed skill</th>
              <th className="px-4 py-3 text-left">Source</th>
              <th className="px-4 py-3 text-left">Repeats</th>
              <th className="px-4 py-3 text-left">Status</th>
              <th className="px-4 py-3 text-left">Proposed</th>
              <th className="px-4 py-3 text-left">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {loading ? (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-gray-400 text-sm">Loading…</td></tr>
            ) : visible.length === 0 ? (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-gray-400 text-sm">No proposals</td></tr>
            ) : visible.map(item => (
              <Fragment key={item.id}>
                <tr className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium text-gray-800">
                    <button onClick={() => setExpanded(expanded === item.id ? null : item.id)}
                      className="text-left hover:underline">
                      {item.proposed_name}
                    </button>
                    {item.department && (
                      <span className="ml-2 text-xs text-gray-400">{item.department}</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">{(item.source || "").replace(/_/g," ")}</td>
                  <td className="px-4 py-3 text-gray-600 text-xs">{item.occurrence_count}×</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${PROPOSAL_STATUS_COLORS[item.status] || ""}`}>
                      {(item.status || "").replace(/_/g," ")}
                    </span>
                    {item.skill_status && (
                      <span className={`ml-1 px-2 py-0.5 rounded-full text-xs font-medium ${SKILL_STATUS_COLORS[item.skill_status] || "bg-gray-100 text-gray-500"}`}>
                        {item.skill_status.replace(/_/g," ")}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-400 text-xs">{toISTDate(item.created_at)}</td>
                  <td className="px-4 py-3">
                    <div className="flex gap-1.5 flex-wrap">
                      {canApprove && item.skill_name && item.skill_status === "PENDING_APPROVAL" && (
                        <>
                          <button disabled={busy === item.skill_name}
                            onClick={() => approveAndPromote(item.skill_name)}
                            className="px-2 py-1 bg-green-600 text-white text-xs rounded-md hover:bg-green-700 disabled:opacity-40">
                            {busy === item.skill_name ? "…" : "Approve & Promote"}
                          </button>
                          <button disabled={busy === item.skill_name}
                            onClick={() => { setRejectModal({ skill_name: item.skill_name }); setRejectReason(""); }}
                            className="px-2 py-1 bg-red-100 text-red-600 text-xs rounded-md hover:bg-red-200 disabled:opacity-40">
                            Reject
                          </button>
                        </>
                      )}
                      {canApprove && item.skill_name && item.skill_status === "APPROVED" && (
                        <button disabled={busy === item.skill_name}
                          onClick={() => promote(item.skill_name)}
                          className="px-2 py-1 bg-purple-600 text-white text-xs rounded-md hover:bg-purple-700 disabled:opacity-40">
                          → PROD
                        </button>
                      )}
                      {item.skill_status === "PRODUCTION" && (
                        <span className="text-xs text-green-600">Live</span>
                      )}
                      {!item.skill_name && (
                        <span className="text-xs text-gray-400">—</span>
                      )}
                    </div>
                  </td>
                </tr>
                {expanded === item.id && (
                  <tr className="bg-gray-50/60">
                    <td colSpan={6} className="px-4 py-3">
                      <div className="text-xs text-gray-600 space-y-2">
                        <div>
                          <span className="font-medium text-gray-500">Representative task: </span>
                          {item.representative_prompt || "—"}
                        </div>
                        {Array.isArray(item.tool_sequence) && item.tool_sequence.length > 0 && (
                          <div>
                            <span className="font-medium text-gray-500">Tools observed: </span>
                            {item.tool_sequence.join(" → ")}
                          </div>
                        )}
                        {item.skill_name && (
                          <div>
                            <span className="font-medium text-gray-500">Skill: </span>
                            <code className="text-gray-700">{item.skill_name}</code>
                            {" "}({item.skill_type})
                          </div>
                        )}
                        {item.resolved_at && (
                          <div className="text-gray-400">Resolved {toISTDate(item.resolved_at)}</div>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>

      {!canApprove && (
        <p className="text-xs text-gray-400 mt-3">
          You can review proposals for your department. Approval requires approver rights.
        </p>
      )}

      {/* Reject modal */}
      {rejectModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md">
            <h2 className="text-base font-semibold mb-3">
              Reject — {rejectModal.skill_name}
            </h2>
            <textarea
              value={rejectReason}
              onChange={e => setRejectReason(e.target.value)}
              placeholder="Reason (required)"
              className="w-full border border-gray-200 rounded-lg p-3 text-sm h-24 resize-none focus:outline-none focus:ring-2 focus:ring-red-300"
            />
            <div className="flex gap-2 mt-4 justify-end">
              <button onClick={() => setRejectModal(null)}
                className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">
                Cancel
              </button>
              <button
                disabled={!rejectReason.trim()}
                onClick={async () => {
                  await reject(rejectModal.skill_name, rejectReason);
                  setRejectModal(null);
                }}
                className="px-4 py-2 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed">
                Confirm Reject
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
