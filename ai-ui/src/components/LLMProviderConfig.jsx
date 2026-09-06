// SPDX-License-Identifier: MIT
import { Fragment, useEffect, useState, useCallback } from "react";
import {
  Server,
  PlusCircle,
  Trash2,
  Pencil,
  ToggleLeft,
  ToggleRight,
  Shield,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  X,
  Check,
  KeyRound,
  Cpu,
  Zap,
  Download,
  Star,
} from "lucide-react";
import { API_BASE, authFetch } from "../config";
import { usePermission } from "../hooks/usePermission";
import { useToast, useConfirm } from "./ui/DialogProvider.jsx";

const JSON_HEADERS = { "Content-Type": "application/json" };

function slugify(name) {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}

// ---------------------------------------------------------------------------
// Family badge
// ---------------------------------------------------------------------------

const FAMILY_LABELS = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  gemini: "Gemini",
  openai_compatible: "OpenAI-compatible",
  ollama: "Ollama",
};

function FamilyBadge({ family }) {
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border bg-indigo-50 text-indigo-700 border-indigo-200">
      {FAMILY_LABELS[family] || family}
    </span>
  );
}

function StatusDot({ status }) {
  const cls =
    status === "ok" ? "bg-green-500" : status === "error" ? "bg-red-500" : "bg-gray-300";
  const label =
    status === "ok" ? "Last verified OK" : status === "error" ? "Last verification failed" : "Not verified yet";
  return <span className={`inline-block w-2 h-2 rounded-full ${cls}`} title={label} />;
}

// ---------------------------------------------------------------------------
// Provider Create / Edit Modal
// ---------------------------------------------------------------------------

function ProviderModal({ provider, families, onSave, onClose }) {
  const isEdit = !!provider;
  const [name, setName] = useState(provider?.name || "");
  const [slug, setSlug] = useState(provider?.slug || "");
  const [slugManuallyEdited, setSlugManuallyEdited] = useState(isEdit);
  const [family, setFamily] = useState(provider?.family || families[0]?.family || "");
  const [baseUrl, setBaseUrl] = useState(provider?.base_url || "");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const meta = families.find((f) => f.family === family) || {};

  const handleNameChange = (v) => {
    setName(v);
    if (!slugManuallyEdited) setSlug(slugify(v));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    if (!name.trim()) return setError("Name is required.");
    if (!slug.trim()) return setError("Slug is required.");
    if (meta.requires_base_url && !baseUrl.trim()) return setError("Base URL is required for this provider family.");
    if (!isEdit && meta.requires_api_key && !apiKey.trim()) return setError("API key is required for this provider family.");

    setSaving(true);
    try {
      const body = { name: name.trim(), base_url: baseUrl.trim() || null };
      if (apiKey.trim()) body.api_key = apiKey.trim();

      let resp;
      if (isEdit) {
        resp = await authFetch(`${API_BASE}/llm-providers/${provider.id}`, {
          method: "PUT",
          headers: JSON_HEADERS,
          body: JSON.stringify(body),
        });
      } else {
        resp = await authFetch(`${API_BASE}/llm-providers/`, {
          method: "POST",
          headers: JSON_HEADERS,
          body: JSON.stringify({ ...body, slug: slug.trim(), family }),
        });
      }
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${resp.status}`);
      }
      const data = await resp.json().catch(() => ({}));
      onSave(isEdit ? null : data.provider);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-md p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-gray-900">
            {isEdit ? "Edit Provider" : "Add LLM Provider"}
          </h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => handleNameChange(e.target.value)}
              placeholder="e.g. Anthropic (Prod)"
              className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
            />
          </div>

          {!isEdit && (
            <>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Slug</label>
                <input
                  type="text"
                  value={slug}
                  onChange={(e) => { setSlug(slugify(e.target.value)); setSlugManuallyEdited(true); }}
                  placeholder="anthropic-prod"
                  className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm font-mono focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Provider Family</label>
                <select
                  value={family}
                  onChange={(e) => setFamily(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                >
                  {families.map((f) => (
                    <option key={f.family} value={f.family}>{f.label}</option>
                  ))}
                </select>
              </div>
            </>
          )}

          {(meta.requires_base_url || isEdit) && (
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Base URL {meta.requires_base_url && <span className="text-red-500">*</span>}
              </label>
              <input
                type="text"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder={family === "ollama" ? "http://ollama:11434" : "https://openrouter.ai/api/v1"}
                className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm font-mono focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
              />
            </div>
          )}

          {(meta.requires_api_key || isEdit) && (
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                API Key {!isEdit && meta.requires_api_key && <span className="text-red-500">*</span>}
              </label>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={isEdit ? "Leave blank to keep the current key" : "sk-..."}
                className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm font-mono focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
              />
              {isEdit && provider?.credential_configured && (
                <p className="text-xs text-gray-400 mt-1 flex items-center gap-1">
                  <KeyRound size={11} /> A key is already configured.
                </p>
              )}
            </div>
          )}

          {error && <p className="text-sm text-red-600">{error}</p>}

          <div className="flex gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 py-2 px-4 border border-gray-200 rounded-lg text-sm font-medium text-gray-600 hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="flex-1 py-2 px-4 brand-grad hover:opacity-70 text-white rounded-lg font-medium text-sm disabled:opacity-50"
            >
              {saving ? "Saving..." : isEdit ? "Save Changes" : "Add Provider"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Model row + inline add form
// ---------------------------------------------------------------------------

function ModelRow({ model, onToggle, onDelete, onSetDefault }) {
  const caps = model.capabilities || {};
  return (
    <tr className="hover:bg-gray-50">
      <td className="px-3 py-2 font-mono text-xs text-gray-800">{model.model_id}</td>
      <td className="px-3 py-2 text-sm text-gray-700">{model.display_name}</td>
      <td className="px-3 py-2 text-xs text-gray-500">
        {caps.context_window ? `${(caps.context_window / 1000).toFixed(0)}K ctx` : "—"}
        {caps.billing_tier === "free" && <span className="ml-1.5 text-green-600">free</span>}
      </td>
      <td className="px-3 py-2 text-xs text-gray-400">{model.source}</td>
      <td className="px-3 py-2">
        <button onClick={() => onToggle(model)} className="text-gray-400 hover:text-indigo-600">
          {model.enabled ? <ToggleRight size={20} className="text-indigo-600" /> : <ToggleLeft size={20} />}
        </button>
      </td>
      <td className="px-3 py-2">
        <button
          onClick={() => onSetDefault(model)}
          disabled={model.is_default}
          title={
            model.is_default
              ? "This is the platform default model — used by Agent Studio, the CLI, and Buddy whenever no model is explicitly selected."
              : model.enabled
              ? "Set as the platform default model"
              : "Set as the platform default model (enable it first for this to take effect)"
          }
          className={model.is_default ? "text-amber-500 cursor-default" : "text-gray-300 hover:text-amber-500"}
        >
          <Star size={16} fill={model.is_default ? "currentColor" : "none"} />
        </button>
      </td>
      <td className="px-3 py-2 text-right">
        <button onClick={() => onDelete(model)} className="p-1 rounded text-gray-400 hover:text-red-600 hover:bg-red-50">
          <Trash2 size={14} />
        </button>
      </td>
    </tr>
  );
}

function AddModelForm({ providerId, onAdded, onCancel, toast }) {
  const [modelId, setModelId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [contextWindow, setContextWindow] = useState("");
  const [saving, setSaving] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!modelId.trim() || !displayName.trim()) return;
    setSaving(true);
    try {
      const capabilities = {};
      if (contextWindow) capabilities.context_window = parseInt(contextWindow, 10);
      const resp = await authFetch(`${API_BASE}/llm-providers/${providerId}/models`, {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ model_id: modelId.trim(), display_name: displayName.trim(), capabilities }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${resp.status}`);
      }
      setModelId(""); setDisplayName(""); setContextWindow("");
      onAdded();
    } catch (err) {
      toast.error("Failed to add model: " + err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="flex items-center gap-2 px-3 py-2 bg-gray-50 border-t border-gray-200">
      <input
        type="text" value={modelId} onChange={(e) => setModelId(e.target.value)}
        placeholder="model id (e.g. claude-sonnet-4-6)"
        className="flex-1 px-2 py-1.5 border border-gray-200 rounded text-xs font-mono"
      />
      <input
        type="text" value={displayName} onChange={(e) => setDisplayName(e.target.value)}
        placeholder="display name"
        className="flex-1 px-2 py-1.5 border border-gray-200 rounded text-xs"
      />
      <input
        type="number" value={contextWindow} onChange={(e) => setContextWindow(e.target.value)}
        placeholder="context window"
        className="w-32 px-2 py-1.5 border border-gray-200 rounded text-xs"
      />
      <button type="submit" disabled={saving} className="p-1.5 rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50">
        <Check size={14} />
      </button>
      <button type="button" onClick={onCancel} className="p-1.5 rounded text-gray-400 hover:text-gray-600">
        <X size={14} />
      </button>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Ollama "pull a new model" — kicks off an async pull job on the backend and
// polls its status, since a model download can take minutes.
// ---------------------------------------------------------------------------

function OllamaPullForm({ providerId, onDone, toast }) {
  const [modelName, setModelName] = useState("");
  const [suggestions, setSuggestions] = useState([]);
  const [job, setJob] = useState(null);   // {status, detail, percent}
  const [pulling, setPulling] = useState(false);

  useEffect(() => {
    authFetch(`${API_BASE}/llm-providers/ollama-suggestions`)
      .then((r) => r.json())
      .then((d) => setSuggestions(d.suggestions || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!job || !job.jobId || job.status === "done" || job.status === "error") return;
    const t = setTimeout(async () => {
      try {
        const resp = await authFetch(`${API_BASE}/llm-providers/${providerId}/pull-status/${job.jobId}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        setJob({ ...data, jobId: job.jobId });
        if (data.status === "done") { toast.success(`Pulled '${modelName}'.`); onDone(); }
        if (data.status === "error") toast.error(`Pull failed: ${data.detail || "unknown error"}`);
      } catch (err) {
        setJob({ status: "error", detail: err.message, jobId: job.jobId });
      }
    }, 1500);
    return () => clearTimeout(t);
  }, [job, providerId, onDone, toast, modelName]);

  const startPull = async (name) => {
    const trimmed = (name || modelName).trim();
    if (!trimmed) return;
    setPulling(true);
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/${providerId}/pull-model`, {
        method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ model_id: trimmed }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      setJob({ status: "pulling", percent: 0, jobId: data.job_id });
    } catch (err) {
      toast.error("Failed to start pull: " + err.message);
    } finally {
      setPulling(false);
    }
  };

  return (
    <div className="px-3 py-2 border-t border-gray-200 bg-white">
      <div className="flex items-center gap-2">
        <input
          type="text" list="ollama-suggestions" value={modelName}
          onChange={(e) => setModelName(e.target.value)}
          placeholder="model name to pull (e.g. llama3.2)"
          className="flex-1 px-2 py-1.5 border border-gray-200 rounded text-xs font-mono"
        />
        <datalist id="ollama-suggestions">
          {suggestions.map((s) => <option key={s.name} value={s.name}>{s.label}</option>)}
        </datalist>
        <button
          onClick={() => startPull()}
          disabled={pulling || (job && job.status === "pulling")}
          className="flex items-center gap-1 px-2.5 py-1.5 rounded bg-indigo-600 text-white text-xs hover:bg-indigo-700 disabled:opacity-50"
        >
          <Download size={13} /> Pull
        </button>
      </div>
      {suggestions.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1.5">
          {suggestions.map((s) => (
            <button
              key={s.name}
              onClick={() => { setModelName(s.name); startPull(s.name); }}
              disabled={pulling || (job && job.status === "pulling")}
              className="text-xs px-2 py-0.5 rounded-full border border-gray-200 text-gray-500 hover:bg-gray-50 disabled:opacity-50"
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
      {job && (
        <div className="mt-2 text-xs">
          {job.status === "pulling" && (
            <div className="flex items-center gap-2 text-gray-500">
              <RefreshCw size={12} className="animate-spin" />
              {job.detail || "Pulling…"} {job.percent != null ? `(${job.percent}%)` : ""}
            </div>
          )}
          {job.status === "done" && <div className="text-green-600">Pull complete.</div>}
          {job.status === "error" && <div className="text-red-600">Pull failed: {job.detail}</div>}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// OpenAI-compatible (OpenRouter/custom) "add by name" — these catalogs can run
// into the hundreds of unrelated-vendor models (OpenRouter alone exposes
// ~480), so there is no bulk "Sync models" for this family (see
// routers/llm_provider_admin_router.py's sync_models — it rejects
// openai_compatible with a 422). Instead: fetch the catalog once as
// autocomplete suggestions, admin types/picks one name, add exactly that one.
// ---------------------------------------------------------------------------

function TypeaheadAddModelForm({ providerId, onAdded, toast }) {
  const [modelName, setModelName] = useState("");
  const [suggestions, setSuggestions] = useState([]);
  const [loadingSuggestions, setLoadingSuggestions] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    authFetch(`${API_BASE}/llm-providers/${providerId}/discover-models`)
      .then((r) => r.json())
      .then((d) => { if (!cancelled) setSuggestions(d.models || []); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoadingSuggestions(false); });
    return () => { cancelled = true; };
  }, [providerId]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    const trimmed = modelName.trim();
    if (!trimmed) return;
    setSaving(true);
    try {
      const match = suggestions.find((s) => s.model_id === trimmed);
      const resp = await authFetch(`${API_BASE}/llm-providers/${providerId}/models`, {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({
          model_id: trimmed,
          display_name: match?.display_name || trimmed,
          capabilities: match?.capabilities || {},
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      setModelName("");
      toast.success(`Added "${trimmed}".`);
      onAdded();
    } catch (err) {
      toast.error("Failed to add model: " + err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="px-3 py-2 border-t border-gray-200 bg-white">
      <div className="flex items-center gap-2">
        <input
          type="text" list={`openai-compat-suggestions-${providerId}`} value={modelName}
          onChange={(e) => setModelName(e.target.value)}
          placeholder={loadingSuggestions ? "Loading catalog…" : "type a model name (e.g. meta-llama/llama-3.1-70b-instruct)"}
          className="flex-1 px-2 py-1.5 border border-gray-200 rounded text-xs font-mono"
        />
        <datalist id={`openai-compat-suggestions-${providerId}`}>
          {suggestions.map((s) => <option key={s.model_id} value={s.model_id} />)}
        </datalist>
        <button
          type="submit"
          disabled={saving || !modelName.trim()}
          className="flex items-center gap-1 px-2.5 py-1.5 rounded bg-indigo-600 text-white text-xs hover:bg-indigo-700 disabled:opacity-50"
        >
          <PlusCircle size={13} /> Add
        </button>
      </div>
      {!loadingSuggestions && (
        <p className="text-xs text-gray-400 mt-1">
          {suggestions.length} model(s) available from this provider — start typing to filter, or paste a name directly.
        </p>
      )}
    </form>
  );
}

// ---------------------------------------------------------------------------
// Provider drill-in (models table)
// ---------------------------------------------------------------------------

function ProviderModels({ provider, toast, confirm }) {
  const [models, setModels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/${provider.id}/models`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setModels(data.models || []);
    } catch (err) {
      toast.error("Failed to load models: " + err.message);
    } finally {
      setLoading(false);
    }
  }, [provider.id, toast]);

  useEffect(() => { load(); }, [load]);

  const handleToggle = async (model) => {
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/models/${model.id}`, {
        method: "PUT", headers: JSON_HEADERS, body: JSON.stringify({ enabled: !model.enabled }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      load();
    } catch (err) {
      toast.error("Failed to update model: " + err.message);
    }
  };

  const handleSetDefault = async (model) => {
    if (model.is_default) return;
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/models/${model.id}`, {
        method: "PUT", headers: JSON_HEADERS, body: JSON.stringify({ is_default: true }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      toast.success(`"${model.display_name}" is now the platform default model.`);
      load();
    } catch (err) {
      toast.error("Failed to set default model: " + err.message);
    }
  };

  const handleDelete = async (model) => {
    const ok = await confirm({
      title: "Delete Model",
      message: `Delete "${model.display_name}" (${model.model_id})? Anything still referencing this model id will need to be updated separately.`,
      confirmLabel: "Delete",
      variant: "danger",
    });
    if (!ok) return;
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/models/${model.id}`, { method: "DELETE" });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${resp.status}`);
      }
      load();
    } catch (err) {
      toast.error("Failed to delete model: " + err.message);
    }
  };

  const handleSync = async () => {
    setSyncing(true);
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/${provider.id}/sync-models`, { method: "POST" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      toast.success(`Sync complete — ${data.added.length} added, ${data.updated.length} updated.`);
      load();
    } catch (err) {
      toast.error("Sync failed: " + err.message);
    } finally {
      setSyncing(false);
    }
  };

  const discoveredCount = models.filter((m) => m.source === "discovered").length;

  const handleClearDiscovered = async () => {
    const ok = await confirm({
      title: "Remove Discovered Models",
      message: `Remove all ${discoveredCount} auto-discovered model(s) from this provider? Manually-added models are not affected.`,
      confirmLabel: "Remove",
      variant: "danger",
    });
    if (!ok) return;
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/${provider.id}/models?source=discovered`, { method: "DELETE" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      toast.success(`Removed ${data.deleted} model(s).`);
      load();
    } catch (err) {
      toast.error("Failed to clear models: " + err.message);
    }
  };

  return (
    <div className="bg-gray-50/50 border-t border-gray-100">
      <div className="flex items-center justify-end gap-3 px-3 pt-2">
        {discoveredCount > 0 && (
          <button
            onClick={handleClearDiscovered}
            className="flex items-center gap-1.5 text-xs text-red-500 hover:text-red-700"
          >
            <Trash2 size={12} /> Remove {discoveredCount} discovered
          </button>
        )}
        {provider.family !== "openai_compatible" && (
          <button
            onClick={handleSync}
            disabled={syncing}
            className="flex items-center gap-1.5 text-xs text-indigo-600 hover:text-indigo-800 disabled:opacity-50"
          >
            <RefreshCw size={12} className={syncing ? "animate-spin" : ""} /> Sync models from provider
          </button>
        )}
      </div>
      {loading ? (
        <div className="text-center py-6 text-gray-400 text-sm">
          <RefreshCw size={16} className="animate-spin mx-auto mb-1" /> Loading models…
        </div>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left">
              <th className="px-3 py-1.5 text-xs font-semibold text-gray-400 uppercase">Model ID</th>
              <th className="px-3 py-1.5 text-xs font-semibold text-gray-400 uppercase">Display Name</th>
              <th className="px-3 py-1.5 text-xs font-semibold text-gray-400 uppercase">Capabilities</th>
              <th className="px-3 py-1.5 text-xs font-semibold text-gray-400 uppercase">Source</th>
              <th className="px-3 py-1.5 text-xs font-semibold text-gray-400 uppercase">Enabled</th>
              <th className="px-3 py-1.5 text-xs font-semibold text-gray-400 uppercase">Default</th>
              <th></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {models.length === 0 && (
              <tr><td colSpan={7} className="text-center py-6 text-gray-400 text-xs">No models yet — add one below.</td></tr>
            )}
            {models.map((m) => (
              <ModelRow key={m.id} model={m} onToggle={handleToggle} onDelete={handleDelete} onSetDefault={handleSetDefault} />
            ))}
          </tbody>
        </table>
      )}
      {provider.family === "ollama" && (
        <OllamaPullForm providerId={provider.id} toast={toast} onDone={load} />
      )}
      {provider.family === "openai_compatible" ? (
        <TypeaheadAddModelForm providerId={provider.id} toast={toast} onAdded={load} />
      ) : showAdd ? (
        <AddModelForm providerId={provider.id} toast={toast} onCancel={() => setShowAdd(false)} onAdded={() => { setShowAdd(false); load(); }} />
      ) : (
        <button
          onClick={() => setShowAdd(true)}
          className="w-full text-left px-3 py-2 text-xs text-indigo-600 hover:bg-indigo-50 flex items-center gap-1.5"
        >
          <PlusCircle size={13} /> Add model manually
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function LLMProviderConfig({ user }) {
  const { isAdmin } = usePermission(user);
  const { toast } = useToast();
  const { confirm } = useConfirm();

  const [providers, setProviders] = useState([]);
  const [families, setFamilies] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreateEdit, setShowCreateEdit] = useState(false);
  const [editingProvider, setEditingProvider] = useState(null);
  const [expandedId, setExpandedId] = useState(null);

  const loadProviders = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setProviders(data.providers || []);
    } catch (err) {
      toast.error("Failed to load providers: " + err.message);
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    if (!isAdmin) return;
    loadProviders();
    authFetch(`${API_BASE}/llm-providers/families`)
      .then((r) => r.json())
      .then((d) => setFamilies(d.families || []))
      .catch(() => {});
  }, [isAdmin, loadProviders]);

  const handleSave = (createdProvider) => {
    setShowCreateEdit(false);
    setEditingProvider(null);
    loadProviders();
    // Newly created providers auto-sync their model catalog and get a
    // platform default assigned server-side (see create_provider in
    // routers/llm_provider_admin_router.py) — surface that so it's clear no
    // further "connect" step is needed.
    if (createdProvider) {
      const count = createdProvider.model_count || 0;
      toast.success(
        count > 0
          ? `"${createdProvider.name}" connected — ${count} model(s) synced automatically.`
          : `"${createdProvider.name}" added. Could not auto-sync models — check the key and use "Sync models".`
      );
    }
  };

  const handleToggle = async (p) => {
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/${p.id}/toggle`, { method: "PATCH" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      toast.success("Provider updated.");
      loadProviders();
    } catch (err) {
      toast.error("Failed to toggle provider: " + err.message);
    }
  };

  const handleTestConnection = async (p) => {
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/${p.id}/test-connection`, { method: "POST" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      if (data.status === "ok") toast.success(data.detail || "Connection OK.");
      else toast.error(data.detail || "Connection failed.");
      loadProviders();
    } catch (err) {
      toast.error("Test connection failed: " + err.message);
    }
  };

  const handleDelete = async (p) => {
    const ok = await confirm({
      title: "Delete Provider",
      message: `Delete "${p.name}"? ${p.model_count > 0 ? `This provider has ${p.model_count} model(s) — they will be deleted too.` : ""}`,
      confirmLabel: "Delete",
      variant: "danger",
    });
    if (!ok) return;
    try {
      const resp = await authFetch(`${API_BASE}/llm-providers/${p.id}?cascade=true`, { method: "DELETE" });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${resp.status}`);
      }
      toast.success("Provider deleted.");
      loadProviders();
    } catch (err) {
      toast.error("Failed to delete provider: " + err.message);
    }
  };

  if (!isAdmin) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <Shield size={40} className="text-gray-300 mx-auto mb-3" />
          <p className="text-gray-500 font-medium">Admin access required</p>
          <p className="text-gray-400 text-sm mt-1">Only admins can manage LLM providers.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="border-b border-gray-200 p-4 flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-3">
          <Server size={18} className="text-indigo-600" />
          <div>
            <h1 className="text-sm font-semibold text-indigo-700">LLM Providers</h1>
            <p className="text-xs text-gray-400">
              Configure LLM providers and models — this is the source of every model shown to users.
            </p>
          </div>
        </div>
        <button
          onClick={() => { setEditingProvider(null); setShowCreateEdit(true); }}
          className="flex items-center gap-1.5 px-3 py-1.5 brand-grad hover:opacity-70 text-white text-xs rounded"
        >
          <PlusCircle size={13} />
          Add Provider
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-6">
        <div className="border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="w-8"></th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Provider</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Family</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Models</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Verified</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Status</th>
                <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {loading ? (
                <tr><td colSpan={7} className="text-center py-12 text-gray-400">
                  <RefreshCw size={20} className="animate-spin mx-auto mb-2" /> Loading providers…
                </td></tr>
              ) : providers.length === 0 ? (
                <tr><td colSpan={7} className="text-center py-16">
                  <Server size={36} className="text-gray-200 mx-auto mb-3" />
                  <p className="text-gray-500 font-medium">No providers configured</p>
                  <p className="text-gray-400 text-xs mt-1">Click "Add Provider" to get started.</p>
                </td></tr>
              ) : providers.map((p) => (
                <Fragment key={p.id}>
                  <tr className="hover:bg-gray-50 transition-colors">
                    <td className="px-2 py-3 text-center">
                      <button onClick={() => setExpandedId(expandedId === p.id ? null : p.id)} className="text-gray-400 hover:text-gray-600">
                        {expandedId === p.id ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                      </button>
                    </td>
                    <td className="px-4 py-3">
                      <div className="font-medium text-gray-800">{p.name}</div>
                      <div className="text-xs text-gray-400 font-mono">{p.slug}</div>
                    </td>
                    <td className="px-4 py-3"><FamilyBadge family={p.family} /></td>
                    <td className="px-4 py-3 text-gray-600 flex items-center gap-1.5">
                      <Cpu size={13} className="text-gray-400" /> {p.model_count}
                    </td>
                    <td className="px-4 py-3"><StatusDot status={p.last_verify_status} /></td>
                    <td className="px-4 py-3">
                      <button onClick={() => handleToggle(p)} className="flex items-center gap-1.5">
                        {p.enabled ? (
                          <ToggleRight size={20} className="text-indigo-600" />
                        ) : (
                          <ToggleLeft size={20} className="text-gray-300" />
                        )}
                        <span className={`text-xs ${p.enabled ? "text-indigo-600" : "text-gray-400"}`}>
                          {p.enabled ? "Enabled" : "Disabled"}
                        </span>
                      </button>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-1">
                        <button
                          onClick={() => handleTestConnection(p)}
                          title="Test connection"
                          className="p-1.5 rounded-lg text-gray-400 hover:text-amber-600 hover:bg-amber-50"
                        >
                          <Zap size={15} />
                        </button>
                        <button
                          onClick={() => { setEditingProvider(p); setShowCreateEdit(true); }}
                          title="Edit provider"
                          className="p-1.5 rounded-lg text-gray-400 hover:text-indigo-600 hover:bg-indigo-50"
                        >
                          <Pencil size={15} />
                        </button>
                        <button
                          onClick={() => handleDelete(p)}
                          title="Delete provider"
                          className="p-1.5 rounded-lg text-gray-400 hover:text-red-600 hover:bg-red-50"
                        >
                          <Trash2 size={15} />
                        </button>
                      </div>
                    </td>
                  </tr>
                  {expandedId === p.id && (
                    <tr>
                      <td colSpan={7} className="p-0">
                        <ProviderModels provider={p} toast={toast} confirm={confirm} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {showCreateEdit && (
        <ProviderModal
          provider={editingProvider}
          families={families}
          onSave={handleSave}
          onClose={() => { setShowCreateEdit(false); setEditingProvider(null); }}
        />
      )}
    </div>
  );
}
