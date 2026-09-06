// SPDX-License-Identifier: MIT
import { useState, useEffect } from "react";
import { API_BASE as API, authFetch } from "../config";
import {
  MessageSquare, CheckCircle, XCircle, AlertCircle,
  RefreshCw, Copy, ExternalLink, Zap, Clock, Terminal,
  Loader2, BarChart2,
} from "lucide-react";

export default function TeamsConfig({ user }) {
  const [config, setConfig]   = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied]   = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [cfgRes, mRes] = await Promise.all([
        authFetch(`${API}/teams/config`),
        authFetch(`${API}/teams/metrics`),
      ]);
      if (cfgRes.ok)  setConfig(await cfgRes.json());
      if (mRes.ok)    setMetrics(await mRes.json());
    } catch (e) {
      console.error("TeamsConfig load error:", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const copyWebhook = () => {
    if (!config?.webhook_url) return;
    navigator.clipboard.writeText(config.webhook_url);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }

  const isConfigured = config?.configured;
  const successRate  = metrics ? Math.round((metrics.success_rate ?? 0) * 100) : 0;

  return (
    <div className="flex flex-col h-full overflow-auto bg-white text-gray-800 p-6 gap-6">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <MessageSquare className="w-6 h-6 text-indigo-600" />
          <div>
            <h1 className="text-xl font-semibold text-gray-900">Microsoft Teams Bot</h1>
            <p className="text-sm text-gray-500">Configure and monitor the AiNxt Teams integration</p>
          </div>
        </div>
        <button
          onClick={load}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm border border-gray-200 bg-white hover:bg-gray-50 text-gray-700 rounded-lg transition"
        >
          <RefreshCw className="w-4 h-4" /> Refresh
        </button>
      </div>

      {/* Status banner */}
      <div className={`flex items-center gap-3 p-4 rounded-xl border ${
        isConfigured
          ? "bg-green-50 border-green-200"
          : "bg-yellow-50 border-yellow-200"
      }`}>
        {isConfigured
          ? <CheckCircle className="w-5 h-5 text-green-600 shrink-0" />
          : <AlertCircle className="w-5 h-5 text-yellow-600 shrink-0" />}
        <div>
          <p className={`font-medium text-sm ${isConfigured ? "text-green-800" : "text-yellow-800"}`}>
            {isConfigured ? "Bot is configured and ready" : "Bot credentials not fully configured"}
          </p>
          <p className="text-xs text-gray-600 mt-0.5">
            {isConfigured
              ? "TEAMS_BOT_APP_ID and TEAMS_BOT_SECRET are set. Register the webhook URL in Azure Bot Service."
              : "Set TEAMS_BOT_APP_ID and TEAMS_BOT_SECRET environment variables to enable the Teams bot."}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">

        {/* Configuration */}
        <div className="bg-white border border-gray-200 rounded-xl p-5">
          <h2 className="text-sm font-semibold text-gray-800 mb-4 flex items-center gap-2">
            <Zap className="w-4 h-4 text-indigo-600" /> Bot Registration
          </h2>
          <div className="space-y-3">
            <Row label="App ID" value={config?.app_id_preview} ok={config?.app_id_preview !== "not set"} />
            <Row label="App Secret" value={config?.secret_set ? "••••••••" : "not set"} ok={config?.secret_set} />
            <Row label="Auth mode" value={config?.skip_auth ? "skip (dev)" : "JWT validation (prod)"} ok={!config?.skip_auth} warn={config?.skip_auth} />

            <div className="pt-2">
              <p className="text-xs text-gray-500 mb-1.5">Webhook URL (register in Azure Bot Service)</p>
              <div className="flex items-center gap-2 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
                <code className="text-xs text-indigo-700 flex-1 truncate">{config?.webhook_url}</code>
                <button onClick={copyWebhook} className="text-gray-500 hover:text-gray-800 transition shrink-0">
                  {copied ? <CheckCircle className="w-4 h-4 text-green-600" /> : <Copy className="w-4 h-4" />}
                </button>
              </div>
            </div>
          </div>

          <div className="mt-4 pt-4 border-t border-gray-100">
            <p className="text-xs text-gray-500 mb-2">Setup steps</p>
            <ol className="text-xs text-gray-600 space-y-1.5 list-decimal list-inside">
              <li>Create an Azure Bot Service resource in the Azure Portal</li>
              <li>Set the Messaging Endpoint to the webhook URL above</li>
              <li>Copy the App ID and Secret → set as env vars and restart</li>
              <li>Install the bot into your Teams channel via App Studio</li>
            </ol>
          </div>
        </div>

        {/* Metrics */}
        <div className="bg-white border border-gray-200 rounded-xl p-5">
          <h2 className="text-sm font-semibold text-gray-800 mb-4 flex items-center gap-2">
            <BarChart2 className="w-4 h-4 text-indigo-600" /> Live Metrics
          </h2>
          {metrics ? (
            <div className="grid grid-cols-2 gap-3">
              <MetricTile label="Messages received" value={metrics.requests_total} />
              <MetricTile label="Agent runs" value={metrics.agent_runs_total} />
              <MetricTile label="Successes" value={metrics.success_total} color="text-green-600" />
              <MetricTile label="Failures" value={metrics.failure_total} color="text-red-600" />
              <MetricTile label="Success rate" value={`${successRate}%`} color={successRate >= 80 ? "text-green-600" : "text-yellow-600"} />
              <MetricTile label="Avg latency" value={`${metrics.avg_latency_ms ?? 0} ms`} />
            </div>
          ) : (
            <p className="text-sm text-gray-500">No metrics available yet.</p>
          )}

          <div className="mt-4 pt-4 border-t border-gray-100 flex items-center gap-2">
            <Clock className="w-4 h-4 text-yellow-600 shrink-0" />
            <div>
              <p className="text-xs text-gray-600">HITL Pending Approvals</p>
              <p className="text-lg font-bold text-yellow-700">{config?.hitl_pending ?? 0}</p>
            </div>
            {(config?.hitl_pending ?? 0) > 0 && (
              <p className="text-xs text-gray-500 ml-auto">
                Approve/reject via Teams Adaptive Cards
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Command Reference */}
      <div className="bg-white border border-gray-200 rounded-xl p-5">
        <h2 className="text-sm font-semibold text-gray-800 mb-4 flex items-center gap-2">
          <Terminal className="w-4 h-4 text-indigo-600" /> Supported Commands
        </h2>
        <div className="space-y-2">
          {(config?.commands ?? []).map(c => (
            <div key={c.cmd} className="flex items-start gap-3 py-2 border-b border-gray-100 last:border-0">
              <code className="text-xs text-indigo-700 bg-gray-50 border border-gray-200 px-2 py-1 rounded font-mono shrink-0 whitespace-nowrap">
                {c.cmd}
              </code>
              <p className="text-xs text-gray-600 pt-1">{c.desc}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Azure Bot Service link */}
      <div className="p-3 bg-gray-50 border border-gray-200 rounded-lg text-xs text-gray-500 flex items-center gap-2">
        <ExternalLink className="w-3.5 h-3.5 shrink-0" />
        Register the bot at{" "}
        <a
          href="https://portal.azure.com/#create/Microsoft.BotServiceConnectivityGalleryPackage"
          target="_blank"
          rel="noopener noreferrer"
          className="text-indigo-600 hover:underline ml-1"
        >
          Azure Bot Service
        </a>{" "}
        — use the webhook URL above as the Messaging Endpoint.
      </div>
    </div>
  );
}

function Row({ label, value, ok, warn }) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-gray-500">{label}</span>
      <span className={`flex items-center gap-1.5 ${warn ? "text-yellow-700" : ok ? "text-green-700" : "text-red-700"}`}>
        {warn
          ? <AlertCircle className="w-3.5 h-3.5" />
          : ok
            ? <CheckCircle className="w-3.5 h-3.5" />
            : <XCircle className="w-3.5 h-3.5" />}
        {value}
      </span>
    </div>
  );
}

function MetricTile({ label, value, color = "text-gray-900" }) {
  return (
    <div className="bg-gray-50 border border-gray-100 rounded-lg p-3">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={`text-xl font-bold ${color}`}>{value}</p>
    </div>
  );
}
