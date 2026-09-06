// SPDX-License-Identifier: MIT
import { useState, useEffect, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { API_BASE as API, authFetch } from "../config";
import { validateFreeText, validateURLField } from "../utils/securityValidation";
import {
  Plug, CheckCircle, XCircle, AlertCircle, RefreshCw,
  Zap, Trash2, Plus, ChevronDown, ChevronUp, Settings,
  Loader2, ExternalLink, BarChart2
} from "lucide-react";

const CATEGORY_LABELS = {
  all: "All",
  communication: "Communication",
  productivity: "Productivity",
  devtools: "Dev Tools",
  hr: "HR",
  crm: "CRM",
  custom: "Custom",
};

const CATEGORY_ORDER = ["all", "communication", "productivity", "devtools", "hr", "crm", "custom"];

const CONNECTOR_ICONS = {
  microsoft_365: "🏢",
  gmail: "📧",
  slack: "💬",
  github: "🐙",
  jira_connector: "🎫",
  gitlab: "🦊",
};

// Only these connectors are visible in the UI. Add more here when they are
// ready to be enabled for users.
const ENABLED_CONNECTORS = new Set(["microsoft_365", "gitlab", "jira_connector", "gmail"]);

export default function Connectors({ user }) {
  const navigate = useNavigate();
  const [available, setAvailable] = useState([]);
  const [status, setStatus] = useState({});
  const [selectedCategory, setSelectedCategory] = useState("all");
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState({});
  const [testResults, setTestResults] = useState({});
  const [showAddForm, setShowAddForm] = useState(false);
  const [newConnector, setNewConnector] = useState({ name: "", display_name: "", base_url: "", auth_type: "oauth2", category: "custom" });
  const [expandedTools, setExpandedTools] = useState({});
  const [oauthMessages, setOauthMessages] = useState({});

  const popupRefs = useRef({});
  const pollRefs = useRef({});
  const latestStatusRef = useRef({});

  const isAdmin = user?.role === "admin";

  const load = useCallback(async () => {
    try {
      const [availRes, statusRes] = await Promise.all([
        authFetch(`${API}/connectors/available`),
        authFetch(`${API}/connectors/status`),
      ]);
      if (availRes.ok) {
        const arr = await availRes.json();
        // Only overwrite when we actually got connectors. A re-fetch on window
        // focus (alt/ctrl-tab) can transiently hit a gateway worker whose
        // in-memory registry is empty; without this guard the list would blank
        // out and "reappear" — the flicker. Keep the last good list instead.
        if (Array.isArray(arr) && arr.length) setAvailable(arr);
      }
      if (statusRes.ok) {
        const statusArr = await statusRes.json();
        const statusMap = {};
        statusArr.forEach(s => { statusMap[s.name] = s; });
        latestStatusRef.current = statusMap;
        setStatus(statusMap);
      }

    } catch (e) {
      console.error("Connectors load error:", e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const clearOAuthPoll = useCallback((connectorName) => {
    const timer = pollRefs.current[connectorName];
    if (timer) clearInterval(timer);
    delete pollRefs.current[connectorName];
    delete popupRefs.current[connectorName];
  }, []);

  const completeOAuth = useCallback(async (connectorName, success, error = "") => {
    if (!connectorName) return;
    clearOAuthPoll(connectorName);
    setActionLoading(p => ({ ...p, [connectorName]: null }));
    setOauthMessages(p => ({
      ...p,
      [connectorName]: success
        ? { success: true, text: "Connected successfully." }
        : { success: false, text: error || "Authentication did not complete." },
    }));
    await load();
    setTimeout(() => setOauthMessages(p => ({ ...p, [connectorName]: null })), 8000);
  }, [clearOAuthPoll, load]);

  // OAuth callback may land directly on /connectors when popups cannot close.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("connected");
    const failed = params.get("connector");
    if (connected) completeOAuth(connected, true);
    else if (failed) completeOAuth(failed, false, params.get("error") || "Authentication failed.");
  }, [completeOAuth]);

  useEffect(() => {
    const onMessage = (event) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data || {};
      if (data.type !== "ainxt:connector-oauth") return;
      completeOAuth(data.connector, !!data.success, data.error || "Authentication failed.");
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [completeOAuth]);

  useEffect(() => {
    return () => {
      Object.values(pollRefs.current).forEach((timer) => clearInterval(timer));
      pollRefs.current = {};
      popupRefs.current = {};
    };
  }, []);

  // The OAuth popup completes in its OWN window, so its `?connected=` param never
  // reaches this screen — the card would stay "Not connected" until a manual
  // reload. Re-fetch status whenever this window regains focus (i.e. after the
  // user returns from the Microsoft popup), so the card flips automatically.
  useEffect(() => {
    // G21: debounce so a burst of focus/visibility events (alt-tabbing) doesn't
    // fire a storm of status fetches — one refetch per 1.5s window is plenty.
    let t = null;
    const onFocus = () => {
      if (t) return;
      t = setTimeout(() => { t = null; load(); }, 1500);
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      if (t) clearTimeout(t);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [load]);

  // PAT-based connect (GitLab, Jira) — reads token from user's Profile vault,
  // no OAuth popup needed. Returns 428 if the token is not yet set in Profile.
  const handlePatConnect = useCallback(async (connector) => {
    const connectorName = connector.name;
    setActionLoading(p => ({ ...p, [connectorName]: "connecting" }));
    setOauthMessages(p => ({ ...p, [connectorName]: null }));
    try {
      const res = await authFetch(`${API}/connectors/pat-connect/${connectorName}`, { method: "POST" });
      const data = await res.json();

      if (res.status === 428) {
        // Token not set in Profile — prompt the user to add it first.
        setOauthMessages(p => ({
          ...p,
          [connectorName]: {
            success: false,
            text: data.message || "Token not found in your profile.",
            showProfileLink: true,
          },
        }));
        return;
      }
      if (!res.ok) {
        setOauthMessages(p => ({
          ...p,
          [connectorName]: { success: false, text: data.detail || "Connection failed." },
        }));
        return;
      }
      await load();
      setOauthMessages(p => ({
        ...p,
        [connectorName]: { success: true, text: "Connected successfully." },
      }));
      setTimeout(() => setOauthMessages(p => ({ ...p, [connectorName]: null })), 8000);
    } catch (e) {
      setOauthMessages(p => ({
        ...p,
        [connectorName]: { success: false, text: e.message || "Connection failed." },
      }));
    } finally {
      setActionLoading(p => ({ ...p, [connectorName]: null }));
    }
  }, [load]);

  const handleConnect = async (connector) => {
    // PAT-based connectors (GitLab, Jira) skip the OAuth popup entirely.
    if (connector.auth_type === "pat") {
      return handlePatConnect(connector);
    }

    // OAuth2 connectors (Microsoft 365, etc.) — existing popup flow.
    const connectorName = connector.name;
    setActionLoading(p => ({ ...p, [connectorName]: "connecting" }));
    setOauthMessages(p => ({ ...p, [connectorName]: null }));
    clearOAuthPoll(connectorName);
    try {
      const res = await authFetch(`${API}/connectors/oauth/start/${connectorName}`);
      if (!res.ok) {
        const err = await res.json();
        setActionLoading(p => ({ ...p, [connectorName]: null }));
        setOauthMessages(p => ({ ...p, [connectorName]: { success: false, text: err.detail || "Unable to start authentication." } }));
        return;
      }
      const { authorize_url } = await res.json();
      const winName = `ainxt_oauth_${connectorName}`;
      const popup = window.open(authorize_url, winName, "width=600,height=700,noopener,noreferrer");
      if (popup) {
        try { popup.opener = null; } catch { /* cross-origin frames may block this — noopener flag above still applies */ }
        try { popup.focus(); } catch { /* ignore */ }
      }
      // Do NOT abort if popup is null — some browsers/engines return null for
      // noopener popups even when the window opened successfully. The polling
      // loop below is the actual source of truth for OAuth completion; it runs
      // regardless of whether we got a popup handle back.
      popupRefs.current[connectorName] = popup;

      let polls = 0;
      const timer = setInterval(async () => {
        await load();
        polls++;
        if (latestStatusRef.current[connectorName]?.connected) {
          await completeOAuth(connectorName, true);
          return;
        }
        const closed = popup ? popup.closed : false;
        if (closed) {
          await load();
          if (latestStatusRef.current[connectorName]?.connected) await completeOAuth(connectorName, true);
          else await completeOAuth(connectorName, false, "Authentication window closed before completion.");
          return;
        }
        if (polls >= 90) await completeOAuth(connectorName, false, "Authentication timed out. Please try again.");
      }, 2000);
      pollRefs.current[connectorName] = timer;
    } catch (e) {
      await completeOAuth(connectorName, false, e.message || "Connection failed.");
    }
  };

  const handleDisconnect = async (connectorName) => {
    if (!confirm(`Disconnect ${connectorName}? This will remove your stored credentials.`)) return;
    setActionLoading(p => ({ ...p, [connectorName]: "disconnecting" }));
    try {
      await authFetch(`${API}/connectors/${connectorName}`, { method: "DELETE" });
      await load();
    } catch (e) {
      alert(`Disconnect failed: ${e.message}`);
    } finally {
      setActionLoading(p => ({ ...p, [connectorName]: null }));
    }
  };

  const handleTest = async (connectorName) => {
    setActionLoading(p => ({ ...p, [connectorName]: "testing" }));
    try {
      const res = await authFetch(`${API}/connectors/${connectorName}/test`);
      const result = await res.json();
      setTestResults(p => ({ ...p, [connectorName]: result }));
      setTimeout(() => setTestResults(p => ({ ...p, [connectorName]: null })), 8000);
    } catch (e) {
      setTestResults(p => ({ ...p, [connectorName]: { success: false, error: e.message } }));
    } finally {
      setActionLoading(p => ({ ...p, [connectorName]: null }));
    }
  };

  const handleAddCustom = async () => {
    if (!newConnector.name || !newConnector.display_name || !newConnector.base_url) {
      alert("Name, display name and base URL are required.");
      return;
    }

    // Client-side pre-check — mirrors validate_connector_definition_request()
    // in core/security_validation.py: display_name/description via
    // validate_free_text(), icon_url/base_url via validate_url_field(). Note:
    // `name` is intentionally not checked here — the backend doesn't validate
    // it either (ConnectorDefinitionCreate.name passes through unsanitized).
    // The backend (connectors_router.py's POST /connectors/definitions)
    // remains the authoritative enforcer.
    const displayCheck = validateFreeText(newConnector.display_name);
    if (!displayCheck.isValid) { alert(`Display name: ${displayCheck.errors[0]?.message}`); return; }
    if (newConnector.description) {
      const descCheck = validateFreeText(newConnector.description);
      if (!descCheck.isValid) { alert(`Description: ${descCheck.errors[0]?.message}`); return; }
    }
    const baseUrlCheck = validateURLField(newConnector.base_url, "Base URL");
    if (!baseUrlCheck.isValid) { alert(baseUrlCheck.errors[0]?.message); return; }
    if (newConnector.icon_url) {
      const iconCheck = validateURLField(newConnector.icon_url, "Icon URL");
      if (!iconCheck.isValid) { alert(iconCheck.errors[0]?.message); return; }
    }

    try {
      const res = await authFetch(`${API}/connectors/definitions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...newConnector, tools: [], auth_config: {} }),
      });
      if (!res.ok) {
        const err = await res.json();
        alert(`Failed: ${err.detail}`);
        return;
      }
      setShowAddForm(false);
      setNewConnector({ name: "", display_name: "", base_url: "", auth_type: "oauth2", category: "custom" });
      await load();
    } catch (e) {
      alert(`Error: ${e.message}`);
    }
  };



  // Restrict to connectors that are currently enabled for users.
  const enabledConnectors = available.filter(c => ENABLED_CONNECTORS.has(c.name));

  const filteredConnectors = enabledConnectors.filter(c =>
    selectedCategory === "all" || c.category === selectedCategory
  );

  const categories = [...new Set(["all", ...enabledConnectors.map(c => c.category)])];

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full overflow-auto bg-white text-gray-800 p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Plug className="w-6 h-6 text-indigo-600" />
          <div>
            <h1 className="text-xl font-semibold text-gray-900">Connectors</h1>
            <p className="text-sm text-gray-500">Connect AiNxt to any enterprise system</p>
          </div>
        </div>
        {isAdmin && (
          <button
            onClick={() => setShowAddForm(!showAddForm)}
            className="flex items-center gap-2 px-3 py-1.5 text-sm bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg transition"
          >
            <Plus className="w-4 h-4" /> Add Custom
          </button>
        )}
      </div>

      {/* Category filter */}
      <div className="flex gap-2 mb-6 flex-wrap">
        {categories.map(cat => (
          <button
            key={cat}
            onClick={() => setSelectedCategory(cat)}
            className={`px-3 py-1 text-sm rounded-full border transition ${
              selectedCategory === cat
                ? "bg-indigo-600 border-indigo-600 text-white"
                : "border-gray-200 text-gray-600 hover:border-indigo-300"
            }`}
          >
            {CATEGORY_LABELS[cat] || cat}
          </button>
        ))}
      </div>

      {/* Add custom connector form */}
      {showAddForm && isAdmin && (
        <div className="mb-6 p-4 bg-gray-50 border border-gray-200 rounded-xl">
          <h3 className="text-sm font-medium mb-3 text-gray-800">Add Custom Connector</h3>
          <div className="grid grid-cols-2 gap-3">
            <input
              className="bg-white border border-gray-200 rounded px-3 py-1.5 text-sm text-gray-700 outline-none focus:border-gray-400"
              placeholder="ID (e.g., salesforce)"
              value={newConnector.name}
              onChange={e => setNewConnector(p => ({ ...p, name: e.target.value.toLowerCase().replace(/\s/g, "_") }))}
            />
            <input
              className="bg-white border border-gray-200 rounded px-3 py-1.5 text-sm text-gray-700 outline-none focus:border-gray-400"
              placeholder="Display Name (e.g., Salesforce CRM)"
              value={newConnector.display_name}
              onChange={e => setNewConnector(p => ({ ...p, display_name: e.target.value }))}
            />
            <input
              className="bg-white border border-gray-200 rounded px-3 py-1.5 text-sm col-span-2 text-gray-700 outline-none focus:border-gray-400"
              placeholder="Base URL (e.g., https://yourorg.salesforce.com)"
              value={newConnector.base_url}
              onChange={e => setNewConnector(p => ({ ...p, base_url: e.target.value }))}
            />
            <select
              className="bg-white border border-gray-200 rounded px-3 py-1.5 text-sm text-gray-700 outline-none focus:border-gray-400"
              value={newConnector.auth_type}
              onChange={e => setNewConnector(p => ({ ...p, auth_type: e.target.value }))}
            >
              <option value="oauth2">OAuth2</option>
              <option value="api_key">API Key</option>
              <option value="bearer_token">Bearer Token</option>
              <option value="basic_auth">Basic Auth</option>
              <option value="none">No Auth</option>
            </select>
            <select
              className="bg-white border border-gray-200 rounded px-3 py-1.5 text-sm text-gray-700 outline-none focus:border-gray-400"
              value={newConnector.category}
              onChange={e => setNewConnector(p => ({ ...p, category: e.target.value }))}
            >
              {Object.entries(CATEGORY_LABELS).filter(([k]) => k !== "all").map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
          </div>
          <div className="flex gap-2 mt-3">
            <button onClick={handleAddCustom} className="px-3 py-1.5 text-sm bg-indigo-600 hover:bg-indigo-700 text-white rounded">
              Create Connector
            </button>
            <button onClick={() => setShowAddForm(false)} className="px-3 py-1.5 text-sm border border-gray-200 bg-white hover:bg-gray-50 text-gray-700 rounded">
              Cancel
            </button>
          </div>
          <p className="text-xs text-gray-500 mt-2">
            After creating, use the admin API to add tool definitions, or they'll use the generic HTTP adapter automatically.
          </p>
        </div>
      )}

      {/* Connector cards */}
      {filteredConnectors.length === 0 ? (
        <div className="flex flex-col items-center justify-center h-40 text-gray-400">
          <Plug className="w-10 h-10 mb-2 opacity-40" />
          <p>No connectors in this category</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredConnectors.map(connector => {
            const connStatus = status[connector.name];
            const isConnected = connStatus?.connected;
            const isLoading = actionLoading[connector.name];
            const testResult = testResults[connector.name];
            const oauthMessage = oauthMessages[connector.name];
            const toolsExpanded = expandedTools[connector.name];

            return (
              <div
                key={connector.name}
                className={`flex flex-col p-4 rounded-xl border bg-white hover:shadow-sm transition ${
                  isConnected
                    ? "border-green-300"
                    : "border-gray-200 hover:border-indigo-300"
                }`}
              >
                {/* Card header */}
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <span className="text-2xl">{CONNECTOR_ICONS[connector.name] || "🔌"}</span>
                    <div>
                      <h3 className="font-medium text-sm text-gray-900">{connector.display_name}</h3>
                      <span className={`text-xs px-1.5 py-0.5 rounded-full ${
                        isConnected ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"
                      }`}>
                        {isConnected ? "Connected" : "Not connected"}
                      </span>
                    </div>
                  </div>
                  <span className="text-xs text-gray-500 capitalize bg-gray-100 px-2 py-0.5 rounded">
                    {connector.category}
                  </span>
                </div>

                {/* Connected as */}
                {isConnected && connStatus?.connected_as && (
                  <p className="text-xs text-gray-500 mb-2 truncate">
                    As: {connStatus.connected_as}
                    {connStatus.workspace && ` · ${connStatus.workspace}`}
                  </p>
                )}

                {/* OAuth / PAT connect result */}
                {oauthMessage && (
                  <div className={`text-xs p-2 rounded mb-2 ${
                    oauthMessage.success ? "bg-green-50 text-green-700 border border-green-200" : "bg-red-50 text-red-700 border border-red-200"
                  }`}>
                    {oauthMessage.success ? "✓" : "✗"} {oauthMessage.text}
                    {!oauthMessage.success && oauthMessage.showProfileLink && (
                      <button
                        onClick={() => navigate("/profile")}
                        className="ml-1 underline font-medium hover:opacity-75 transition-opacity"
                      >
                        Go to Profile →
                      </button>
                    )}
                  </div>
                )}

                {/* Test result */}
                {testResult && (
                  <div className={`text-xs p-2 rounded mb-2 ${
                    testResult.success ? "bg-green-50 text-green-700 border border-green-200" : "bg-red-50 text-red-700 border border-red-200"
                  }`}>
                    {testResult.success
                      ? `✓ Connected — ${testResult.tool_count ?? connector.tool_count} tools available · ${testResult.items_returned ?? 0} items returned (${testResult.latency_ms}ms)`
                      : `✗ ${testResult.error}`}
                  </div>
                )}

                {/* Tool count + expand */}
                <button
                  onClick={() => setExpandedTools(p => ({ ...p, [connector.name]: !p[connector.name] }))}
                  className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 mb-3 transition"
                >
                  <Zap className="w-3 h-3" />
                  {connector.tool_count} tool{connector.tool_count !== 1 ? "s" : ""}
                  {toolsExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                </button>

                {/* Tool list (expanded) */}
                {toolsExpanded && connector.tools && (
                  <div className="mb-3 space-y-1">
                    {connector.tools.map(t => (
                      <div key={t.name} className="text-xs text-gray-600 bg-gray-50 border border-gray-100 rounded px-2 py-1">
                        <span className="font-mono text-indigo-600">{t.name}</span>
                        {t.description && <span className="ml-1 text-gray-500">— {t.description.slice(0, 60)}{t.description.length > 60 ? "…" : ""}</span>}
                      </div>
                    ))}
                  </div>
                )}

                {/* Actions */}
                <div className="flex gap-2 mt-auto pt-2">
                  {isConnected ? (
                    <>
                      <button
                        onClick={() => handleTest(connector.name)}
                        disabled={!!isLoading}
                        className="flex items-center gap-1 px-2 py-1 text-xs border border-gray-200 bg-white hover:bg-gray-50 text-gray-700 rounded transition disabled:opacity-50"
                      >
                        {isLoading === "testing" ? <Loader2 className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />}
                        Test
                      </button>
                      <button
                        onClick={() => handleDisconnect(connector.name)}
                        disabled={!!isLoading}
                        className="flex items-center gap-1 px-2 py-1 text-xs bg-red-50 hover:bg-red-100 text-red-700 border border-red-200 rounded transition disabled:opacity-50"
                      >
                        {isLoading === "disconnecting" ? <Loader2 className="w-3 h-3 animate-spin" /> : <Trash2 className="w-3 h-3" />}
                        Disconnect
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={() => handleConnect(connector)}
                      disabled={!!isLoading}
                      className="flex items-center gap-1 px-3 py-1 text-xs bg-indigo-600 hover:bg-indigo-700 text-white rounded transition disabled:opacity-50"
                    >
                      {isLoading === "connecting" ? <Loader2 className="w-3 h-3 animate-spin" /> : <ExternalLink className="w-3 h-3" />}
                      Connect
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* How it works note */}
      <div className="mt-6 p-3 bg-gray-50 border border-gray-200 rounded-lg text-xs text-gray-500">
        <strong className="text-gray-700">How it works:</strong> Once connected, ask AiNxt naturally —
        "show me emails from CEO last week", "list my open Jira issues", or "show MRs in project X" — and AiNxt will
        automatically route to the right connector. All data is scanned for PCI/PII before being shared.
        <span className="block mt-1">
          <strong className="text-gray-600">GitLab &amp; Jira</strong> use your Personal Access Token from{" "}
          <button onClick={() => navigate("/profile")} className="underline text-indigo-600 hover:text-indigo-800">
            Profile → API Token Vault
          </button>
          . Re-click Connect after updating your token in Profile.
        </span>
      </div>
    </div>
  );
}
