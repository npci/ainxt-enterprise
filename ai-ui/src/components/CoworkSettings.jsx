// SPDX-License-Identifier: Apache-2.0
// CoworkSettings — "Buddy Setup": self-service personalization prefs +
// (admin) role-specialist packs. Backed by routers/cowork_admin_router.py.
import { useEffect, useState, useCallback } from "react";
import { Briefcase, Save, Plus, Trash2, Loader2, Check, ShieldCheck } from "lucide-react";
import { API_BASE as API, authFetch } from "../config";
import { validateIdentifier, validateFreeText } from "../utils/securityValidation";
import CoworkEnterprise from "./CoworkEnterprise.jsx";

const DOC_FORMATS = ["docx", "pdf", "pptx", "xlsx", "md"];
const PPT_THEMES = ["ainxt_corporate", "dark_executive", "light_modern", "vibrant_tech"];
const TONES = ["formal", "concise", "friendly", "neutral"];

export default function CoworkSettings({ user }) {
  const isAdmin = user?.role === "admin";
  const _cu = typeof window !== "undefined" ? window.ainxtDesktop?.computerUse : null;
  const [cuEnabled, setCuEnabled] = useState(false);
  useEffect(() => { if (_cu) _cu.isEnabled().then(setCuEnabled).catch(() => {}); }, []);
  const toggleCu = async () => { try { setCuEnabled(await _cu.setEnabled(!cuEnabled)); } catch { /* ignore */ } };

  // ── Preferences ───────────────────────────────────────────────────────────
  const [prefs, setPrefs] = useState({ email_signature: "", default_doc_format: "docx", preferred_ppt_theme: "", tone: "" });
  const [savingPrefs, setSavingPrefs] = useState(false);
  const [prefsSaved, setPrefsSaved] = useState(false);

  // ── Roles ─────────────────────────────────────────────────────────────────
  const [roles, setRoles] = useState([]);
  const [connectors, setConnectors] = useState([]);
  const [skills, setSkills] = useState([]);       // available skills (multi-select)
  const [editing, setEditing] = useState(null); // role being created/edited
  const [savingRole, setSavingRole] = useState(false);
  const [err, setErr] = useState("");
  const [mktQuery, setMktQuery] = useState("");   // marketplace search
  const [skillQuery, setSkillQuery] = useState(""); // role-builder skill filter

  // ── Auto-Allow Permissions ────────────────────────────────────────────────
  const [permissions, setPermissions] = useState([]);   // [{connector_name, tool_name, always_allow}]
  const [permBusy, setPermBusy] = useState({});         // key: `${connector}::${tool}`
  // Add-to-allowlist form state
  const [addConnector, setAddConnector] = useState(""); // selected connector in dropdown 1
  const [addTool, setAddTool] = useState("");           // selected tool in dropdown 2
  const [addBusy, setAddBusy] = useState(false);        // spinner while Add API call is in flight

  const load = useCallback(async () => {
    try {
      // Admins manage ALL roles; users get published (org marketplace) + their OWN.
      const rolesUrl = isAdmin ? `${API}/buddy/roles` : `${API}/buddy/roles?published=1`;
      const [p, r, c, s, permsRes] = await Promise.all([
        authFetch(`${API}/buddy/prefs`).then((x) => x.json()).catch(() => ({})),
        authFetch(rolesUrl).then((x) => x.json()).catch(() => ({})),
        authFetch(`${API}/connectors/available`).then((x) => x.json()).catch(() => []),
        authFetch(`${API}/skills`).then((x) => x.json()).catch(() => ({})),
        authFetch(`${API}/connectors/permissions`).then((x) => x.json()).catch(() => []),
      ]);
      if (p?.prefs) setPrefs((s2) => ({ ...s2, ...p.prefs }));
      if (r?.roles) setRoles(r.roles);
      if (Array.isArray(c)) setConnectors(c);
      if (s?.skills) setSkills(s.skills);
      if (Array.isArray(permsRes)) setPermissions(permsRes);
    } catch { /* ignore */ }
  }, [isAdmin]);

  // Remove a permission row entirely (revert to "ask me each time").
  const revokePermission = async (connectorName, toolName) => {
    const key = `${connectorName}::${toolName}`;
    setPermBusy((b) => ({ ...b, [key]: true }));
    try {
      await authFetch(
        `${API}/connectors/permissions/${encodeURIComponent(connectorName)}?tool_name=${encodeURIComponent(toolName)}`,
        { method: "DELETE" },
      );
      await load();
    } catch { /* ignore */ } finally {
      setPermBusy((b) => ({ ...b, [key]: false }));
    }
  };
  useEffect(() => { load(); }, [load]);

  const savePrefs = async () => {
    setSavingPrefs(true); setPrefsSaved(false);
    try {
      const res = await authFetch(`${API}/buddy/prefs`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prefs }),
      });
      if (res.ok) { setPrefsSaved(true); setTimeout(() => setPrefsSaved(false), 2000); }
    } finally { setSavingPrefs(false); }
  };

  const blankRole = () => ({ name: "", description: "", system_prompt: "", allowed_connectors: [], skill_names: [], visibility: "personal" });

  const saveRole = async () => {
    setErr("");
    if (!editing.name.trim() || !editing.system_prompt.trim()) { setErr("Name and system prompt are required."); return; }

    // Client-side pre-check — mirrors the server-side validators in
    // core/security_validation.py (validate_identifier() for name/list items,
    // validate_free_text() for system_prompt/description). The backend
    // (routers/cowork_admin_router.py) remains the authoritative enforcer.
    const nameCheck = validateIdentifier(editing.name);
    if (!nameCheck.isValid) { setErr(nameCheck.errors[0]?.message || "Invalid name"); return; }
    const promptCheck = validateFreeText(editing.system_prompt);
    if (!promptCheck.isValid) { setErr(promptCheck.errors[0]?.message || "Invalid system prompt"); return; }
    const descCheck = validateFreeText(editing.description || "");
    if (!descCheck.isValid) { setErr(descCheck.errors[0]?.message || "Invalid description"); return; }
    for (const slug of editing.allowed_connectors || []) {
      const c = validateIdentifier(slug);
      if (!c.isValid) { setErr(c.errors[0]?.message || "Invalid connector"); return; }
    }
    for (const nm of editing.skill_names || []) {
      const c = validateIdentifier(nm);
      if (!c.isValid) { setErr(c.errors[0]?.message || "Invalid skill name"); return; }
    }

    setSavingRole(true);
    try {
      const body = { ...editing, skill_names: editing.skill_names || [] };
      delete body._skills_csv;
      const url = editing.id ? `${API}/buddy/roles/${editing.id}` : `${API}/buddy/roles`;
      const res = await authFetch(url, {
        method: editing.id ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) { setErr((await res.json().catch(() => ({})))?.detail || "Save failed."); return; }
      setEditing(null);
      load();
    } finally { setSavingRole(false); }
  };

  const deleteRole = async (id) => {
    if (!confirm("Delete this role?")) return;
    await authFetch(`${API}/buddy/roles/${id}`, { method: "DELETE" });
    load();
  };

  // Governance: approvers (ad_level ≤ 3 / admin) approve or reject a PENDING role.
  const canApprove = !!user?.can_approve;
  const reviewRole = async (r, action) => {
    await authFetch(`${API}/buddy/roles/${r.id}/${action}`, { method: "POST" });
    load();
  };

  const toggleConnector = (slug) => setEditing((e) => ({
    ...e,
    allowed_connectors: e.allowed_connectors.includes(slug)
      ? e.allowed_connectors.filter((x) => x !== slug)
      : [...e.allowed_connectors, slug],
  }));
  const toggleSkill = (nm) => setEditing((e) => ({
    ...e,
    skill_names: (e.skill_names || []).includes(nm)
      ? e.skill_names.filter((x) => x !== nm)
      : [...(e.skill_names || []), nm],
  }));

  return (
    <div className="h-full overflow-y-auto bg-white text-gray-800 p-6">
     <div className="max-w-4xl mx-auto">
      <div className="flex items-center gap-3 mb-1">
        <Briefcase className="w-6 h-6 text-indigo-600" />
        <h1 className="text-xl font-semibold text-gray-900">Buddy Setup</h1>
      </div>
      <p className="text-sm text-gray-500 mb-6">Personalize how Buddy drafts and works for you{isAdmin ? ", and manage role specialists for your teams." : "."}</p>

      {/* ── My Preferences ───────────────────────────────────────────────── */}
      <section className="max-w-2xl mb-10">
        <h2 className="text-sm font-semibold text-gray-700 mb-3">My preferences</h2>
        <div className="bg-gray-50 border border-gray-200 rounded-xl p-4 space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Email signature</label>
            <textarea rows={3} value={prefs.email_signature || ""}
              onChange={(e) => setPrefs({ ...prefs, email_signature: e.target.value })}
              placeholder={"Regards,\nYour Name\nAiNxt"}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none focus:border-gray-400" />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Default document</label>
              <select value={prefs.default_doc_format || "docx"} onChange={(e) => setPrefs({ ...prefs, default_doc_format: e.target.value })}
                className="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-sm bg-white outline-none">
                {DOC_FORMATS.map((f) => <option key={f} value={f}>{f}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">PPT theme</label>
              <select value={prefs.preferred_ppt_theme || ""} onChange={(e) => setPrefs({ ...prefs, preferred_ppt_theme: e.target.value })}
                className="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-sm bg-white outline-none">
                <option value="">(default)</option>
                {PPT_THEMES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Tone</label>
              <select value={prefs.tone || ""} onChange={(e) => setPrefs({ ...prefs, tone: e.target.value })}
                className="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-sm bg-white outline-none">
                <option value="">(default)</option>
                {TONES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
          </div>
          <button onClick={savePrefs} disabled={savingPrefs}
            className="flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white disabled:opacity-50">
            {savingPrefs ? <Loader2 className="w-4 h-4 animate-spin" /> : prefsSaved ? <Check className="w-4 h-4" /> : <Save className="w-4 h-4" />}
            {prefsSaved ? "Saved" : "Save preferences"}
          </button>
        </div>
      </section>

      {/* ── Auto-Allow Permissions ───────────────────────────────────────── */}
      {(() => {
        const ENABLED = new Set(["microsoft_365", "gitlab", "jira_connector"]);
        const ICONS = { microsoft_365: "🏢", gmail: "📧", slack: "💬", github: "🐙", jira_connector: "🎫", gitlab: "🦊" };

        const enabledConns = connectors.filter((c) => ENABLED.has(c.name));

        // Build a lookup: connector → tool → permission row
        const permMap = {};
        permissions.forEach((p) => {
          if (!permMap[p.connector_name]) permMap[p.connector_name] = {};
          permMap[p.connector_name][p.tool_name] = p;
        });

        // Collect ALL rows (one per tool per connector + wildcard rows)
        const allRows = enabledConns.flatMap((conn) => {
          const tools = Array.isArray(conn.tools) ? conn.tools : [];
          const rows = tools.map((t) => {
            const stored = permMap[conn.name]?.[t.name];
            return {
              connector: conn.name,
              displayName: conn.display_name || conn.name,
              icon: ICONS[conn.name] || "🔌",
              tool: t.name,
              toolDesc: t.description || "",
              always_allow: stored?.always_allow ?? false,
              hasRow: !!stored,
            };
          });
          // Also add a wildcard "All tools" row if it exists in permissions
          const wildcard = permMap[conn.name]?.["*"];
          if (wildcard) {
            rows.unshift({
              connector: conn.name,
              displayName: conn.display_name || conn.name,
              icon: ICONS[conn.name] || "🔌",
              tool: "*",
              toolDesc: "Applies to all tools of this connector",
              always_allow: wildcard.always_allow,
              hasRow: true,
            });
          }
          return rows;
        });

        // Only show rows that are actually allowed in the table
        const allowedRows = allRows.filter((r) => r.always_allow);
        const autoAllowCount = allowedRows.length;

        // Tools available for the selected connector in the add-form dropdown
        const addConnectorObj = enabledConns.find((c) => c.name === addConnector);
        const addToolOptions = addConnectorObj
          ? [{ name: "*", description: "Applies to every tool of this connector" }, ...(Array.isArray(addConnectorObj.tools) ? addConnectorObj.tools : [])]
          : [];

        // Handler: add a new entry to the allowlist
        const handleAdd = async () => {
          if (!addConnector || !addTool) return;
          setAddBusy(true);
          try {
            await authFetch(`${API}/connectors/permissions`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ connector: addConnector, tool: addTool, always_allow: true }),
            });
            await load();
            setAddConnector("");
            setAddTool("");
          } catch { /* ignore */ } finally {
            setAddBusy(false);
          }
        };

        return (
          <section className="max-w-4xl mb-10">
            {/* Section header */}
            <div className="flex items-center gap-2 mb-1">
              <ShieldCheck className="w-4 h-4 text-indigo-600" />
              <h2 className="text-sm font-semibold text-gray-700">Auto-allow permissions</h2>
              {autoAllowCount > 0 && (
                <span className="text-xs bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded-full font-medium">
                  {autoAllowCount} allowed
                </span>
              )}
            </div>
            <p className="text-xs text-gray-500 mb-3 max-w-2xl">
              Allow Buddy and scheduled tasks to use a connector tool without asking each time.
              Permissions set from Buddy chat appear here automatically.
            </p>

            {enabledConns.length === 0 ? (
              <div className="bg-gray-50 border border-gray-200 rounded-xl p-4 text-xs text-gray-400">
                No connectors available. Connect a service from the Connectors page first.
              </div>
            ) : (
              <>
                {/* ── Add-to-allowlist form ── */}
                <div className="flex items-end gap-2 mb-4 flex-wrap">
                  {/* Dropdown 1: Connector */}
                  <div className="flex flex-col gap-1">
                    <label className="text-xs font-medium text-gray-600">Connector</label>
                    <select
                      value={addConnector}
                      onChange={(e) => { setAddConnector(e.target.value); setAddTool(""); }}
                      className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm bg-white outline-none focus:border-indigo-400 min-w-[180px]"
                    >
                      <option value="">Select connector…</option>
                      {enabledConns.map((c) => (
                        <option key={c.name} value={c.name}>
                          {ICONS[c.name] ? `${ICONS[c.name]} ` : ""}{c.display_name || c.name}
                        </option>
                      ))}
                    </select>
                  </div>

                  {/* Dropdown 2: Tool (enabled only after connector is selected) */}
                  <div className="flex flex-col gap-1">
                    <label className={`text-xs font-medium ${addConnector ? "text-gray-600" : "text-gray-400"}`}>Tool</label>
                    <select
                      value={addTool}
                      onChange={(e) => setAddTool(e.target.value)}
                      disabled={!addConnector}
                      className={`border border-gray-200 rounded-lg px-2 py-1.5 text-sm bg-white outline-none focus:border-indigo-400 min-w-[220px] ${
                        !addConnector ? "opacity-50 cursor-not-allowed" : ""
                      }`}
                    >
                      <option value="">Select tool…</option>
                      {addToolOptions.map((t) => (
                        <option key={t.name} value={t.name}>
                          {t.name === "*" ? "★ All tools" : t.name}
                        </option>
                      ))}
                    </select>
                  </div>

                  {/* Add button */}
                  <button
                    onClick={handleAdd}
                    disabled={!addConnector || !addTool || addBusy}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {addBusy
                      ? <Loader2 className="w-4 h-4 animate-spin" />
                      : <Plus className="w-4 h-4" />
                    }
                    Add to allowlist
                  </button>
                </div>

                {/* ── Allowed-only table ── */}
                <div className="bg-gray-50 border border-gray-200 rounded-xl overflow-hidden">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-gray-200 bg-white">
                        <th className="text-left px-4 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wide w-1/4">Connector</th>
                        <th className="text-left px-4 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wide">Tool</th>
                        <th className="text-left px-4 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wide">Description</th>
                        <th className="px-4 py-2.5 w-12"></th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                      {allowedRows.length === 0 && (
                        <tr>
                          <td colSpan={4} className="px-4 py-6 text-xs text-gray-400 text-center">
                            No tools auto-allowed yet — use the dropdowns above to add one.
                          </td>
                        </tr>
                      )}
                      {allowedRows.map((row) => {
                        const key = `${row.connector}::${row.tool}`;
                        const busy = !!permBusy[key];
                        return (
                          <tr key={key} className="hover:bg-white transition-colors bg-indigo-50/30">
                            {/* Connector */}
                            <td className="px-4 py-2.5">
                              <div className="flex items-center gap-1.5">
                                <span className="text-base leading-none">{row.icon}</span>
                                <span className="text-xs font-medium text-gray-700 truncate">{row.displayName}</span>
                              </div>
                            </td>

                            {/* Tool name */}
                            <td className="px-4 py-2.5">
                              {row.tool === "*" ? (
                                <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-indigo-50 border border-indigo-100 text-indigo-600 text-xs font-medium">
                                  All tools
                                </span>
                              ) : (
                                <span className="font-mono text-xs text-gray-800">{row.tool}</span>
                              )}
                            </td>

                            {/* Description */}
                            <td className="px-4 py-2.5">
                              <span className="text-[11px] text-gray-400 truncate max-w-xs block">
                                {row.tool === "*"
                                  ? "Applies to every tool of this connector"
                                  : row.toolDesc
                                    ? `${row.toolDesc.slice(0, 80)}${row.toolDesc.length > 80 ? "…" : ""}`
                                    : "—"
                                }
                              </span>
                            </td>

                            {/* Remove button */}
                            <td className="px-4 py-2.5 text-right">
                              {busy ? (
                                <Loader2 className="w-4 h-4 animate-spin text-indigo-400 ml-auto" />
                              ) : (
                                <button
                                  onClick={() => revokePermission(row.connector, row.tool)}
                                  title="Remove from allowlist"
                                  className="p-1 text-gray-400 hover:text-red-500 transition-colors"
                                >
                                  <Trash2 className="w-4 h-4" />
                                </button>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </section>
        );
      })()}

      {/* ── Plugin marketplace (everyone) ────────────────────────────────── */}
      <section className="mb-10">
        <div className="flex items-center gap-2 mb-1">
          <h2 className="text-sm font-semibold text-gray-700">Plugin marketplace</h2>
          <span className="text-xs text-gray-400">— ready-made Buddy specialists (a prompt + connectors + skills + sub-agents bundled). Pick one in the Buddy role selector to use it.</span>
        </div>
        <input value={mktQuery} onChange={(e) => setMktQuery(e.target.value)} placeholder="Search plugins…"
          className="w-64 border border-gray-200 rounded-lg px-3 py-1.5 text-sm outline-none focus:border-gray-400 mb-3" />
        {roles.length === 0 ? (
          <p className="text-sm text-gray-400">No plugins published yet{isAdmin ? " — create a role below." : "."}</p>
        ) : (
          <div className="grid grid-cols-2 lg:grid-cols-3 gap-2 max-h-[28rem] overflow-y-auto pr-1">
            {roles.filter((r) => r.status === "APPROVED" && r.visibility === "public")
                  .filter((r) => !mktQuery.trim() || `${r.name} ${r.description || ""} ${(r.allowed_connectors||[]).join(" ")} ${(r.skill_names||[]).join(" ")}`.toLowerCase().includes(mktQuery.toLowerCase()))
                  .map((r) => (
              <div key={r.id} className="bg-gray-50 border border-gray-200 rounded-lg p-3 flex flex-col">
                <div className="flex items-center gap-2">
                  <Briefcase className="w-4 h-4 text-indigo-600 shrink-0" />
                  <span className="text-sm font-medium text-gray-800">{r.name}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-green-100 text-green-700 ml-auto">published</span>
                </div>
                <p className="text-xs text-gray-500 mt-1 line-clamp-2">{r.description || "—"}</p>
                {(r.allowed_connectors || []).length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-2">
                    {(r.allowed_connectors || []).map((c) => (
                      <span key={c} className="text-[10px] px-1.5 py-0.5 rounded bg-white border border-gray-200 text-gray-500">{c}</span>
                    ))}
                  </div>
                )}
                {(r.skill_names || []).length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1.5">
                    {(r.skill_names || []).map((s) => (
                      <span key={s} className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-50 border border-indigo-100 text-indigo-600">{s}</span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ── Native computer use (admin, desktop only) ────────────────────── */}
      {isAdmin && _cu && (
        <section className="max-w-2xl mb-10">
          <h2 className="text-sm font-semibold text-gray-700 mb-3">Native computer use (this machine)</h2>
          <div className="bg-amber-50 border border-amber-200 rounded-xl p-4">
            <label className="flex items-start gap-3 cursor-pointer">
              <input type="checkbox" checked={cuEnabled} onChange={toggleCu} className="mt-1" />
              <span>
                <span className="text-sm font-medium text-gray-800">Allow Buddy to control this computer</span>
                <span className="block text-xs text-gray-500 mt-1">
                  Lets the agent move the mouse, type, and read the screen to drive desktop/legacy apps.
                  High-risk: every action needs your confirmation, and screenshots are PAN/PII-redacted at the
                  gateway before the model sees them. Requires Git/native deps installed + OS Accessibility &amp;
                  Screen-Recording permissions.
                </span>
              </span>
            </label>
          </div>
        </section>
      )}

      {/* ── Role specialists — anyone can build a PRIVATE one; admins PUBLISH org-wide ── */}
      {(
        <section>
          <div className="flex items-center gap-2 mb-1">
            <h2 className="text-sm font-semibold text-gray-700">{isAdmin ? "Role specialists" : "My role specialists"}</h2>
            <button onClick={() => setEditing(blankRole())}
              className="ml-auto flex items-center gap-1.5 text-sm px-2.5 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-700">
              <Plus className="w-4 h-4 text-indigo-600" /> New role
            </button>
          </div>
          <p className="text-xs text-gray-500 mb-3 max-w-2xl">
            A <b>role</b> turns Buddy into a specialist for a job — e.g. "Settlement Ops Assistant" or
            "Exec Assistant". It bundles a system prompt + the connectors it may use + skills. It works with
            <i> only</i> those connectors. Set it <b>Personal</b> (just you) or <b>Private</b> (your department) yourself; {isAdmin
              ? "and you (admin) can Publish one org-wide for everyone."
              : "an admin can Publish it org-wide for the whole company."}
          </p>

          {(() => {
            // Non-admins manage only their OWN roles (the fetch may also carry published org
            // roles for the marketplace section above — exclude those from this list).
            const myId = user?.userId || user?.id || user?.sub || "";
            // Own roles + (for approvers) the pending-approval queue.
            const mine = isAdmin ? roles : roles.filter((r) =>
              (r.created_by || "") === myId || (canApprove && r.status === "PENDING_APPROVAL"));
            if (mine.length === 0 && !editing) return <p className="text-sm text-gray-400">No roles yet — click “New role”.</p>;
            const TIER = { personal: "Personal", private: "Department", public: "Org-wide" };
            const tierBadge = (v) => (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-500">{TIER[v] || v}</span>
            );
            const statusBadge = (st) => {
              if (st === "PENDING_APPROVAL") return <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700">Pending approval</span>;
              if (st === "REJECTED") return <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-red-100 text-red-700">Rejected</span>;
              if (st === "APPROVED") return <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-green-100 text-green-700">Approved</span>;
              return null;
            };
            return (
          <div className="space-y-2">
            {mine.map((r) => (
              <div key={r.id} className="flex items-center gap-3 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-gray-800 flex items-center gap-1.5 flex-wrap">
                    {r.name}
                    {tierBadge(r.visibility)}
                    {/* personal needs no approval, so don't show a status pill for it */}
                    {r.visibility !== "personal" && statusBadge(r.status)}
                  </div>
                  <div className="text-xs text-gray-500 truncate">{r.description || (r.allowed_connectors || []).join(", ")}</div>
                </div>
                {/* Approvers: act on the pending queue (KB-style). */}
                {canApprove && r.status === "PENDING_APPROVAL" && (
                  <div className="ml-auto flex items-center gap-1.5">
                    <button onClick={() => reviewRole(r, "approve")}
                      className="text-xs px-2 py-1 rounded border border-green-200 text-green-700 hover:bg-green-50">Approve</button>
                    <button onClick={() => reviewRole(r, "reject")}
                      className="text-xs px-2 py-1 rounded border border-red-200 text-red-700 hover:bg-red-50">Reject</button>
                  </div>
                )}
                <button onClick={() => setEditing({ ...r, skill_names: r.skill_names || [], allowed_connectors: r.allowed_connectors || [] })}
                  className={`${canApprove && r.status === "PENDING_APPROVAL" ? "" : "ml-auto "}text-xs px-2 py-1 rounded border border-gray-200 hover:bg-white text-gray-600`}>Edit</button>
                <button onClick={() => deleteRole(r.id)} className="p-1 text-gray-400 hover:text-red-600"><Trash2 className="w-4 h-4" /></button>
              </div>
            ))}
          </div>
            );
          })()}

          {editing && (
            <div className="mt-4 bg-white border border-indigo-200 rounded-xl p-4 space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                  placeholder="Role name (e.g. Exec Assistant)"
                  className="border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none focus:border-gray-400" />
                {/* Tiers: Personal (just me — Buddy-specific), Private (my department —
                    same meaning as skills/agents), and Public (org-wide) — admins only.
                    Private & Public go through approval (KB-style); Personal does not. */}
                <select value={(!isAdmin && editing.visibility === "public") ? "private" : (editing.visibility || "personal")}
                  onChange={(e) => setEditing({ ...editing, visibility: e.target.value })}
                  className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm bg-white outline-none">
                  <option value="personal">Personal — just me</option>
                  <option value="private">Private — my department</option>
                  {isAdmin && <option value="public">Public — whole organization</option>}
                </select>
              </div>
              <input value={editing.description} onChange={(e) => setEditing({ ...editing, description: e.target.value })}
                placeholder="Short description"
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none focus:border-gray-400" />
              <textarea rows={4} value={editing.system_prompt} onChange={(e) => setEditing({ ...editing, system_prompt: e.target.value })}
                placeholder="System prompt — who this specialist is and how it should work…"
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none focus:border-gray-400" />
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Allowed connectors</label>
                <div className="flex flex-wrap gap-1.5">
                  {connectors.map((c) => (
                    <button key={c.name} onClick={() => toggleConnector(c.name)}
                      className={`text-xs px-2.5 py-1 rounded-full border ${editing.allowed_connectors.includes(c.name)
                        ? "bg-indigo-50 border-indigo-300 text-indigo-700" : "bg-white border-gray-200 text-gray-500 hover:border-gray-300"}`}>
                      {c.display_name || c.name}
                    </button>
                  ))}
                  {connectors.length === 0 && <span className="text-xs text-gray-400">No connectors available.</span>}
                </div>
              </div>
              {(() => {
                // Office roles only bundle OFFICE skills (behavioral SOPs). Engineering
                // execution skills (deploy_service, fix_bug, code_review…) can't run on
                // the no-code office surface, so they're excluded here.
                const officeSkills = skills.filter((s) => (s.skill_type || "execution") === "behavioral");
                const sel = editing.skill_names || [];
                const q = skillQuery.trim().toLowerCase();
                const shown = officeSkills.filter((s) => !q || `${s.name} ${s.description || ""}`.toLowerCase().includes(q));
                return (
                  <div>
                    <div className="flex items-center gap-2 mb-1">
                      <label className="text-xs font-medium text-gray-600">Skills <span className="text-gray-400 font-normal">(office SOPs the agent follows)</span></label>
                      <span className="text-[11px] text-indigo-600 ml-auto">{sel.length} selected</span>
                    </div>
                    <input value={skillQuery} onChange={(e) => setSkillQuery(e.target.value)} placeholder="Search skills…"
                      className="w-full border border-gray-200 rounded-lg px-3 py-1.5 text-sm outline-none focus:border-gray-400 mb-2" />
                    <div className="border border-gray-200 rounded-lg divide-y divide-gray-100 max-h-56 overflow-y-auto">
                      {shown.length === 0 && (
                        <div className="px-3 py-3 text-xs text-gray-400">
                          {officeSkills.length === 0 ? "No office skills yet — run scripts/seed_cowork_skills.py or create a behavioral skill." : "No skills match your search."}
                        </div>
                      )}
                      {shown.map((s) => {
                        const on = sel.includes(s.name);
                        return (
                          <label key={s.name} title={s.description || ""}
                            className={`flex items-start gap-2.5 px-3 py-2 cursor-pointer hover:bg-gray-50 ${on ? "bg-indigo-50/50" : ""}`}>
                            <input type="checkbox" checked={on} onChange={() => toggleSkill(s.name)} className="mt-0.5 shrink-0" />
                            <span className="min-w-0">
                              <span className="block text-xs font-medium text-gray-800">{s.name}</span>
                              {s.description && <span className="block text-[11px] text-gray-500 line-clamp-2">{s.description}</span>}
                            </span>
                          </label>
                        );
                      })}
                    </div>
                    {sel.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        {sel.map((nm) => (
                          <span key={nm} className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-50 border border-indigo-100 text-indigo-600 flex items-center gap-1">
                            {nm}
                            <button onClick={() => toggleSkill(nm)} className="text-indigo-400 hover:text-indigo-700">×</button>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })()}
              {err && <div className="text-xs text-red-600">{err}</div>}
              <div className="flex justify-end gap-2">
                <button onClick={() => setEditing(null)} className="px-3 py-1.5 text-sm rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50">Cancel</button>
                <button onClick={saveRole} disabled={savingRole}
                  className="px-3 py-1.5 text-sm rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-1.5">
                  {savingRole ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                  {editing.id ? "Update role" : "Create role"}
                </button>
              </div>
            </div>
          )}
        </section>
      )}

      {/* ── Enterprise controls (admin): connector policy, spend limits, usage ── */}
      {isAdmin && <CoworkEnterprise />}
     </div>
    </div>
  );
}
