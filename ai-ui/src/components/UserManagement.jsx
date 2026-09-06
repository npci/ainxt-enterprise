// SPDX-License-Identifier: MIT
import { useState, useEffect } from "react";
import { usePermission } from "../hooks/usePermission";
import { API_BASE, authFetch, apiFetch } from "../config";
import { toISTDate } from "../utils/time";
import { decryptPii } from "../utils/piiCrypto";

const ROLES     = ["viewer","developer","operator","security","admin"];
const ROLE_COLOR = {
  viewer:    "bg-gray-100 text-gray-500",
  user:      "bg-blue-100 text-blue-500",   // legacy
  developer: "bg-blue-100 text-blue-600",
  operator:  "bg-purple-100 text-purple-600",
  security:  "bg-orange-100 text-orange-600",
  admin:     "bg-red-100 text-red-600",
};

export default function UserManagement({ user }) {
  const { isAdmin, isSecurity } = usePermission(user);
  const [users,    setUsers]   = useState([]);
  const [total,    setTotal]   = useState(0);
  const [editUser, setEditUser]= useState(null);
  const [showNew,  setShowNew] = useState(false);
  const [form,     setForm]    = useState({});
  const [error,    setError]   = useState("");
  const [loading,  setLoading] = useState(false);

  const jsonHeaders = { "Content-Type": "application/json" };

  const load = () => {
    if (!isSecurity) return;
    setLoading(true);
    Promise.all([
      authFetch(`${API_BASE}/auth/users`)
        .then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e.detail || `HTTP ${r.status}`))),
      apiFetch(`${API_BASE}/auth/ui-config`).then(r => r.ok ? r.json() : null).catch(() => null),
    ])
      .then(async ([d, uiCfg]) => {
        const piiOn = !!uiCfg?.pii_payload_encryption_enabled;
        const decrypted = await Promise.all((d.users || []).map(async u => ({
          ...u,
          email: await decryptPii(u.email, piiOn),
          name:  await decryptPii(u.name,  piiOn),
        })));
        setUsers(decrypted);
        setTotal(d.total || 0);
      })
      .catch(err => setError(typeof err === "string" ? err : "Failed to load users"))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const patch = async (userId, body) => {
    setError("");
    const r = await authFetch(`${API_BASE}/auth/users/${userId}`, {
      method: "PATCH", headers: jsonHeaders, body: JSON.stringify(body),
    });
    if (!r.ok) { const e = await r.json(); setError(e.detail || "Update failed"); return false; }
    setEditUser(null);
    load();
    return true;
  };

  const create = async () => {
    setError("");
    const r = await authFetch(`${API_BASE}/auth/users`, {
      method: "POST", headers: jsonHeaders, body: JSON.stringify(form),
    });
    if (!r.ok) { const e = await r.json(); setError(e.detail || "Create failed"); return; }
    setShowNew(false);
    setForm({});
    load();
  };

  if (!isSecurity) return (
    <div className="p-8 text-center text-gray-400 text-sm">
      Requires Security or Admin role.
    </div>
  );

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-xl font-semibold text-gray-800">Users</h1>
          <p className="text-xs text-gray-400">{total} registered</p>
        </div>
        {isAdmin && (
          <button onClick={() => { setShowNew(true); setForm({ role: "viewer" }); setError(""); }}
            className="px-4 py-2 bg-gray-800 text-white text-sm rounded-lg hover:bg-gray-700">
            + New User
          </button>
        )}
      </div>

      {error && (
        <div className="mb-3 text-sm text-red-600 bg-red-50 border border-red-200 px-3 py-2 rounded-lg">
          {error}
        </div>
      )}

      <div className="border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
            <tr>
              <th className="px-4 py-3 text-left">Name</th>
              <th className="px-4 py-3 text-left">Email</th>
              <th className="px-4 py-3 text-left">Role</th>
              <th className="px-4 py-3 text-left">Status</th>
              <th className="px-4 py-3 text-left">Joined</th>
              {isAdmin && <th className="px-4 py-3 text-left">Actions</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {loading ? (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-gray-400">Loading…</td></tr>
            ) : users.map(u => (
              <tr key={u.id} className="hover:bg-gray-50">
                <td className="px-4 py-3 font-medium text-gray-800">{u.name}</td>
                <td className="px-4 py-3 text-gray-500 text-xs">{u.email}</td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-0.5 rounded-full text-xs font-medium capitalize ${ROLE_COLOR[u.role] || "bg-gray-100 text-gray-500"}`}>
                    {u.role}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <span className={`text-xs font-medium ${u.is_active ? "text-green-600" : "text-red-500"}`}>
                    {u.is_active ? "Active" : "Disabled"}
                  </span>
                </td>
                <td className="px-4 py-3 text-gray-400 text-xs">
                  {toISTDate(u.created_at) || "—"}
                </td>
                {isAdmin && (
                  <td className="px-4 py-3">
                    <div className="flex gap-3">
                      <button onClick={() => { setEditUser(u); setForm({ role: u.role }); setError(""); }}
                        className="text-xs text-blue-500 hover:underline">
                        Edit role
                      </button>
                      <button onClick={() => patch(u.id, { is_active: !u.is_active })}
                        className={`text-xs hover:underline ${u.is_active ? "text-red-400" : "text-green-500"}`}>
                        {u.is_active ? "Disable" : "Enable"}
                      </button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Edit role modal */}
      {editUser && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl p-6 w-96">
            <h2 className="text-base font-semibold mb-4">Edit Role — {editUser.name}</h2>
            <label className="text-xs text-gray-500 block mb-1">Role</label>
            <select
              value={form.role}
              onChange={e => setForm(f => ({ ...f, role: e.target.value }))}
              className="w-full border border-gray-200 rounded-lg p-2 text-sm mb-4 capitalize"
            >
              {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
            {error && <p className="text-xs text-red-500 mb-3">{error}</p>}
            <div className="flex gap-2 justify-end">
              <button onClick={() => setEditUser(null)} className="px-4 py-2 text-sm text-gray-500">Cancel</button>
              <button onClick={() => patch(editUser.id, { role: form.role })}
                className="px-4 py-2 text-sm bg-gray-800 text-white rounded-lg hover:bg-gray-700">
                Save
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Create user modal */}
      {showNew && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl p-6 w-96">
            <h2 className="text-base font-semibold mb-4">New User</h2>
            {[
              { field: "name",     type: "text",     label: "Full Name" },
              { field: "email",    type: "email",    label: "Email" },
              { field: "password", type: "password", label: "Temporary Password" },
            ].map(({ field, type, label }) => (
              <div key={field} className="mb-3">
                <label className="text-xs text-gray-500 block mb-1">{label}</label>
                <input
                  type={type}
                  value={form[field] || ""}
                  onChange={e => setForm(f => ({ ...f, [field]: e.target.value }))}
                  className="w-full border border-gray-200 rounded-lg p-2 text-sm focus:outline-none focus:ring-2 focus:ring-gray-300"
                />
              </div>
            ))}
            <div className="mb-4">
              <label className="text-xs text-gray-500 block mb-1">Role</label>
              <select
                value={form.role || "viewer"}
                onChange={e => setForm(f => ({ ...f, role: e.target.value }))}
                className="w-full border border-gray-200 rounded-lg p-2 text-sm capitalize"
              >
                {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
            {error && <p className="text-xs text-red-500 mb-3">{error}</p>}
            <div className="flex gap-2 justify-end">
              <button onClick={() => { setShowNew(false); setError(""); }}
                className="px-4 py-2 text-sm text-gray-500">Cancel</button>
              <button onClick={create}
                className="px-4 py-2 text-sm bg-gray-800 text-white rounded-lg hover:bg-gray-700">
                Create
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
