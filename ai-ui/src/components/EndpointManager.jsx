// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState, useCallback, useRef } from "react";
import {
  Globe,
  PlusCircle,
  Trash2,
  Pencil,
  ToggleLeft,
  ToggleRight,
  Shield,
  RefreshCw,
  X,
  AlertTriangle,
  Eye,
  Key,
  Copy,
  Check,
  Cpu,
  Cloud,
  Wallet,
  Search,
} from "lucide-react";
import { API_BASE, authFetch } from "../config";
import { usePermission } from "../hooks/usePermission";
import { useToast, useConfirm } from "./ui/DialogProvider.jsx";
import { validateIdentifier, validateFreeText } from "../utils/securityValidation";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const JSON_HEADERS = { "Content-Type": "application/json" };

function slugify(name) {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 50);
}

function truncate(text, maxLen = 60) {
  if (!text) return "";
  return text.length > maxLen ? text.slice(0, maxLen) + "..." : text;
}

function toEnvKeyName(name) {
  return (
    name
      .toUpperCase()
      .replace(/[^A-Z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 60) + "_LITELLM_API_KEY"
  );
}


function ModelChip({ modelId, isCloud = false }) {
  // Cloud models are visually distinct because selecting one commits real money
  // against the HOD's monthly cap.
  const cls = isCloud
    ? "bg-amber-50 text-amber-800 border-amber-200"
    : "bg-gray-100 text-gray-700 border-gray-200";
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border ${cls}`}>
      {isCloud && <Cloud size={10} />}
      {modelId}
    </span>
  );
}

// One selectable row in the model picker. Cloud rows show per-1M pricing so the
// cost implication is visible at selection time, not after the bill arrives.
function ModelOption({ model, checked, onToggle }) {
  const p = model.pricing;
  return (
    <label
      className={`flex items-center gap-2.5 px-3 py-2 cursor-pointer hover:bg-gray-50 transition-colors ${checked ? "bg-indigo-50" : ""}`}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={() => onToggle(model.id)}
        className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500 flex-shrink-0"
      />
      <span className="flex-1 min-w-0 flex items-center gap-2">
        <ModelChip modelId={model.id} isCloud={model.is_cloud} />
      </span>
      {p && (
        <span className="text-xs text-gray-400 flex-shrink-0 font-mono">
          ${p.input_per_1m}/${p.output_per_1m} per 1M
        </span>
      )}
    </label>
  );
}

// Cap / consumed / remaining for the selected HOD, with the same colour
// thresholds BudgetManager uses (>=90% red, >=70% amber, else green).
function BudgetBanner({ budget }) {
  const cap = Number(budget.cap_usd || 0);
  const consumed = Number(budget.consumed_usd || 0);
  const remaining = Number(budget.remaining_usd || 0);
  const pct = cap > 0 ? Math.min(100, Math.round((consumed / cap) * 100)) : 0;
  const bar = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-yellow-400" : "bg-green-500";

  return (
    <div className="mt-2 p-3 bg-gray-50 border border-gray-200 rounded-lg">
      <div className="flex items-center justify-between text-xs mb-1.5">
        <span className="text-gray-500">
          Monthly budget{budget.period_yyyymm ? ` (${budget.period_yyyymm})` : ""}
        </span>
        <span className="font-mono text-gray-700">
          ${remaining.toFixed(2)} of ${cap.toFixed(2)} left
        </span>
      </div>
      <div className="h-1.5 w-full bg-gray-200 rounded-full overflow-hidden">
        <div className={`h-full ${bar} transition-all`} style={{ width: `${pct}%` }} />
      </div>
      {!budget.has_cap_row && (
        <p className="mt-1.5 text-xs text-amber-700 flex items-center gap-1">
          <AlertTriangle size={11} />
          This HOD has no cap configured — cloud requests will be refused.
        </p>
      )}
      {budget.has_cap_row && remaining <= 0 && (
        <p className="mt-1.5 text-xs text-red-600 flex items-center gap-1">
          <AlertTriangle size={11} />
          Budget exhausted — cloud requests are being refused until{" "}
          {budget.resets_on || "the next period"}.
        </p>
      )}
      {budget.enforcement === false && (
        <p className="mt-1.5 text-xs text-gray-400">
          Enforcement is off (shadow mode) — usage is recorded but never blocked.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Key Reveal Modal — shown once after create or regenerate
// ---------------------------------------------------------------------------

function KeyRevealModal({ rawKey, onClose }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(rawKey).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-lg p-6">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-full bg-amber-100 flex items-center justify-center flex-shrink-0">
            <Key size={20} className="text-amber-600" />
          </div>
          <div>
            <h3 className="text-lg font-semibold text-gray-900">API Key Generated</h3>
            <p className="text-sm text-gray-500">Copy it now — it will not be shown again</p>
          </div>
        </div>

        <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 mb-4 flex items-start gap-2">
          <AlertTriangle size={16} className="text-amber-600 flex-shrink-0 mt-0.5" />
          <p className="text-sm text-amber-800">
            This key is shown <strong>only once</strong>. Store it securely and share it with
            your team. It cannot be recovered — only regenerated (which invalidates the old key).
          </p>
        </div>

        <div className="flex items-center gap-2 mb-6">
          <div className="flex-1 font-mono text-sm bg-gray-50 border border-gray-200 rounded-lg px-3 py-2.5 break-all text-gray-800 select-all">
            {rawKey}
          </div>
          <button
            onClick={handleCopy}
            className="flex-shrink-0 flex items-center gap-1.5 px-3 py-2.5 rounded-lg border border-gray-200 bg-white hover:bg-gray-50 text-sm font-medium text-gray-700 transition-colors"
          >
            {copied ? (
              <><Check size={15} className="text-green-600" /> Copied</>
            ) : (
              <><Copy size={15} /> Copy</>
            )}
          </button>
        </div>

        <button
          onClick={onClose}
          className="w-full py-2.5 px-4 brand-grad hover:opacity-70 text-white rounded-lg font-medium text-sm transition-colors"
        >
          I've copied it — Close
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Models Preview Panel — inline below the row
// ---------------------------------------------------------------------------

function ModelsPreviewPanel({ models, cloudModels = [], onClose }) {
  const cloudSet = new Set(cloudModels);
  return (
    <div className="mt-2 border border-gray-200 rounded-lg bg-gray-50 p-4">
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-semibold text-gray-600 flex items-center gap-1.5">
          <Eye size={13} /> Selected Models
        </span>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
          <X size={14} />
        </button>
      </div>
      {models.length === 0 ? (
        <p className="text-sm text-gray-500 italic">
          No models selected — all local models are allowed. Cloud models require
          an explicit selection.
        </p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {models.map((m) => (
            <ModelChip key={m} modelId={m} isCloud={cloudSet.has(m)} />
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create / Edit Modal
// ---------------------------------------------------------------------------

function EndpointModal({ endpoint, onSave, onClose }) {
  const isEdit = !!endpoint;
  const [name, setName] = useState(endpoint?.name || "");
  const [slug, setSlug] = useState(endpoint?.slug || "");
  const [description, setDescription] = useState(endpoint?.description || "");
  const [useEnvKey, setUseEnvKey] = useState(endpoint?.use_env_key ?? false);
  const [envKeyName, setEnvKeyName] = useState(endpoint?.env_key_name || "");
  const [toolCallsEnabled, setToolCallsEnabled] = useState(endpoint?.tool_calls_enabled ?? true);
  const [selectedModels, setSelectedModels] = useState(endpoint?.model_ids || []);
  const [showModelPreview, setShowModelPreview] = useState(false);
  const [cloudModels, setCloudModels] = useState([]);   // [{id, is_cloud, pricing}]
  const [localModels, setLocalModels] = useState([]);   // [{id, is_cloud, pricing}]
  const [modelsLoading, setModelsLoading] = useState(false);
  // Fallback for an unrecognised model is COMPUTED (local-first, else cheapest
  // cloud) — see fallbackPreview below — never admin-selected.
  // HOD budget owner — required once any cloud model is selected.
  const [hodEmail, setHodEmail] = useState(endpoint?.hod_email || "");
  const [hods, setHods] = useState([]);
  const [hodsLoading, setHodsLoading] = useState(false);
  const [hodQuery, setHodQuery] = useState("");
  const [hodOpen, setHodOpen] = useState(false);
  const hodRef = useRef(null);
  const [slugManuallyEdited, setSlugManuallyEdited] = useState(isEdit);
  const [envKeyManuallyEdited, setEnvKeyManuallyEdited] = useState(isEdit);
  const [saving, setSaving] = useState(false);
  const [formErrors, setFormErrors] = useState({ name: "", slug: "", envKeyName: "", models: "", hod: "", description: "" });
  const [serverError, setServerError] = useState("");

  // Which of the selected models cost money. Derived, never stored — mirrors the
  // server, where cloud_enabled = model_ids ∩ cloud catalog.
  const cloudIds = new Set(cloudModels.map((m) => m.id));
  const selectedCloud = selectedModels.filter((m) => cloudIds.has(m));
  const cloudSelected = selectedCloud.length > 0;

  // Fetch the full selectable catalog (cloud + local) in one call.
  useEffect(() => {
    setModelsLoading(true);
    authFetch(`${API_BASE}/endpoint-mgmt/available-models`)
      .then((r) => r.json())
      .then((d) => {
        setCloudModels(d.cloud || []);
        setLocalModels(d.local || []);
      })
      .catch(() => {
        setCloudModels([]);
        setLocalModels([]);
      })
      .finally(() => setModelsLoading(false));
  }, []);

  // HOD list (with live budget) for the owner picker.
  useEffect(() => {
    setHodsLoading(true);
    authFetch(`${API_BASE}/endpoint-mgmt/hods`)
      .then((r) => r.json())
      .then((d) => setHods(d.hods || []))
      .catch(() => setHods([]))
      .finally(() => setHodsLoading(false));
  }, []);

  // Close the HOD dropdown on an outside click.
  useEffect(() => {
    const onDocClick = (e) => {
      if (hodRef.current && !hodRef.current.contains(e.target)) setHodOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  const toggleModel = useCallback((id) => {
    setSelectedModels((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
    setFormErrors((prev) => ({ ...prev, models: "" }));
  }, []);

  // Fallback for an unrecognised model, COMPUTED to mirror exactly what the
  // server (endpoint_proxy_router._resolve_model /
  // services.endpoint_model_catalog.cheapest_cloud_model) will actually pick
  // at request time: prefer a local model (free) if one is selected, else the
  // cheapest selected cloud model by (input_per_1m + output_per_1m). Never
  // admin-selected — recomputed live as checkboxes change, same data already
  // loaded for the picker (no extra fetch).
  const fallbackPreview = (() => {
    if (selectedModels.length === 0) return null;
    const localPick = selectedModels.find((m) => !cloudIds.has(m));
    if (localPick) return { model: localPick, kind: "local" };
    const cloudPicks = selectedModels.filter((m) => cloudIds.has(m));
    if (cloudPicks.length === 0) return null;
    const rank = (m) => {
      const p = cloudModels.find((c) => c.id === m)?.pricing;
      return (p?.input_per_1m || 0) + (p?.output_per_1m || 0);
    };
    const cheapest = cloudPicks.reduce((best, m) => (rank(m) < rank(best) ? m : best));
    return { model: cheapest, kind: "cloud" };
  })();

  const selectedHod = hods.find((h) => h.hod_email === hodEmail) || null;
  const filteredHods = hods.filter((h) => {
    if (!hodQuery) return true;
    const q = hodQuery.toLowerCase();
    return (
      (h.hod_email || "").toLowerCase().includes(q) ||
      (h.hod_name || "").toLowerCase().includes(q) ||
      (h.departments || []).some((d) => (d || "").toLowerCase().includes(q))
    );
  });

  const handleNameChange = (v) => {
    setName(v);
    if (!slugManuallyEdited) setSlug(slugify(v));
    if (!envKeyManuallyEdited && useEnvKey) setEnvKeyName(toEnvKeyName(v));
    setFormErrors(prev => ({ ...prev, name: "" }));
  };

  const handleSlugChange = (v) => {
    setSlug(v.toLowerCase().replace(/[^a-z0-9-]/g, ""));
    setSlugManuallyEdited(true);
    setFormErrors(prev => ({ ...prev, slug: "" }));
  };

  const handleEnvKeyChange = (v) => {
    setEnvKeyName(v.toUpperCase().replace(/[^A-Z0-9_]/g, ""));
    setEnvKeyManuallyEdited(true);
    setFormErrors(prev => ({ ...prev, envKeyName: "" }));
  };

  const handleUseEnvKeyToggle = () => {
    const next = !useEnvKey;
    setUseEnvKey(next);
    if (next && !envKeyManuallyEdited && name) {
      setEnvKeyName(toEnvKeyName(name));
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setServerError("");

    const errors = { name: "", slug: "", envKeyName: "", models: "", hod: "", description: "" };
    if (!name.trim()) errors.name = "Name is required.";
    if (!slug.trim()) errors.slug = "Slug is required.";
    if (useEnvKey && !envKeyName.trim()) errors.envKeyName = "Env variable name is required.";
    // At least one model must be selected whenever a catalog is available.
    // NOTE: model_ids=null means "unrestricted" on the server, so an empty
    // selection must never be sent once cloud models exist in the picker.
    if (!modelsLoading && (localModels.length > 0 || cloudModels.length > 0) &&
        selectedModels.length === 0)
      errors.models = "Please select at least one model.";
    // Mirrors the server rule: cloud models need a budget owner.
    if (cloudSelected && !hodEmail)
      errors.hod = "A HOD budget owner is required when cloud models are selected.";

    // Client-side pre-check mirroring validate_endpoint_mgmt_request() in
    // core/security_validation.py — name is an identifier, description is
    // free text. Backend remains the authoritative enforcer.
    if (name.trim()) {
      const nameCheck = validateIdentifier(name.trim());
      if (!nameCheck.isValid) errors.name = nameCheck.errors[0]?.message || "Invalid name.";
    }
    if (description.trim()) {
      const descCheck = validateFreeText(description.trim());
      if (!descCheck.isValid) errors.description = descCheck.errors[0]?.message || "Invalid description.";
    }

    if (Object.values(errors).some(Boolean)) {
      setFormErrors(errors);
      return;
    }
    setFormErrors({ name: "", slug: "", envKeyName: "", models: "", hod: "", description: "" });

    setSaving(true);
    try {
      const common = {
        name: name.trim(),
        description: description.trim() || null,
        use_env_key: useEnvKey,
        env_key_name: useEnvKey ? envKeyName.trim() : null,
        tool_calls_enabled: toolCallsEnabled,
        model_ids: selectedModels.length > 0 ? selectedModels : null,
        // "" clears the owner server-side; only valid when no cloud model remains.
        hod_email: hodEmail || "",
        // No fallback_model — the server computes it from model_ids.
      };
      const payload = isEdit ? common : { ...common, slug: slug.trim() };

      const resp = await authFetch(
        isEdit ? `${API_BASE}/endpoint-mgmt/${endpoint.id}` : `${API_BASE}/endpoint-mgmt/`,
        { method: isEdit ? "PUT" : "POST", headers: JSON_HEADERS, body: JSON.stringify(payload) }
      );

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(
          typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail) || `HTTP ${resp.status}`
        );
      }

      const data = await resp.json();
      onSave(data);
    } catch (err) {
      setServerError(err.message || "Failed to save endpoint.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-lg flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-gray-900">
            {isEdit ? "Edit Endpoint" : "Create Endpoint"}
          </h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors">
            <X size={20} />
          </button>
        </div>

        {/* Body */}
        <form onSubmit={handleSubmit} className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
          {serverError && (
            <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-4 py-3">
              {serverError}
            </div>
          )}

          {/* Name */}
          <div>
            <label className="block text-xs text-gray-500 mb-1">
              Team / Endpoint Name <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => handleNameChange(e.target.value)}
              placeholder="e.g. Platform Team"
              className={`w-full bg-white border rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${
                formErrors.name ? "border-red-500" : "border-gray-300"
              }`}
            />
            {formErrors.name && <p className="mt-1 text-xs text-red-600">{formErrors.name}</p>}
          </div>

          {/* Slug */}
          <div>
            <label className="block text-xs text-gray-500 mb-1">
              Endpoint Slug <span className="text-red-500">*</span>
              {isEdit && (
                <span className="ml-2 text-xs text-gray-400 font-normal">(immutable after creation)</span>
              )}
            </label>
            <div className={`flex items-center bg-white border rounded overflow-hidden focus-within:border-indigo-300 ${
              formErrors.slug ? "border-red-500" : "border-gray-300"
            }`}>
              <span className="px-3 py-2 bg-gray-50 text-gray-400 text-sm border-r border-gray-200 select-none whitespace-nowrap">
                /ainxt/v1/api/
              </span>
              <input
                type="text"
                value={slug}
                onChange={(e) => handleSlugChange(e.target.value)}
                placeholder="lxpendpoint"
                disabled={isEdit}
                className="flex-1 px-3 py-2 text-sm text-gray-900 focus:outline-none disabled:bg-gray-50 disabled:text-gray-500"
              />
            </div>
            {!isEdit && slug && (
              <p className="text-xs text-gray-500 mt-1">
                Proxy URL:{" "}
                <span className="font-mono text-indigo-600">
                  /ainxt/v1/api/{slug}/v1/chat/completions
                </span>
              </p>
            )}
            {formErrors.slug && <p className="mt-1 text-xs text-red-600">{formErrors.slug}</p>}
          </div>

          {/* Description */}
          <div>
            <label className="block text-xs text-gray-500 mb-1">
              Description <span className="text-gray-400 font-normal">(optional)</span>
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="e.g. Shared endpoint for the platform team's internal tools"
              rows={2}
              className="w-full bg-white border border-gray-300 rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 resize-none"
            />
            {formErrors.description && <p className="mt-1 text-xs text-red-600">{formErrors.description}</p>}
          </div>

          {/* Model selection — checkboxes, at least one required */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="block text-xs text-gray-500">
                <span className="flex items-center gap-1">
                  <Cpu size={12} className="text-gray-400" />
                  Allowed Models <span className="text-red-500">*</span>{" "}
                  <span className="text-gray-400 font-normal">(cloud &amp; local)</span>
                </span>
              </label>
              {selectedModels.length > 0 && (
                <button
                  type="button"
                  onClick={() => setShowModelPreview((v) => !v)}
                  className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800 transition-colors"
                >
                  <Eye size={12} />
                  {showModelPreview ? "Hide" : "Preview"} ({selectedModels.length})
                </button>
              )}
            </div>

            {/* Preview panel — selected models as chips */}
            {showModelPreview && selectedModels.length > 0 && (
              <div className="mb-2 p-2.5 bg-indigo-50 border border-indigo-100 rounded-lg flex flex-wrap gap-1.5">
                {selectedModels.map((m) => (
                  <ModelChip key={m} modelId={m} isCloud={cloudIds.has(m)} />
                ))}
              </div>
            )}

            {/* Checkbox list — Cloud section first (they cost money), then Local */}
            {modelsLoading ? (
              <p className="text-xs text-gray-400 flex items-center gap-1 py-2">
                <RefreshCw size={11} className="animate-spin" /> Loading models…
              </p>
            ) : cloudModels.length === 0 && localModels.length === 0 ? (
              <p className="text-xs text-amber-600 py-2">
                No models available — LiteLLM may be unreachable and no cloud models are enabled.
              </p>
            ) : (
              <div className="border border-gray-200 rounded-lg max-h-64 overflow-y-auto">
                {cloudModels.length > 0 && (
                  <>
                    <div className="px-3 py-2 bg-amber-50 border-b border-amber-100 sticky top-0">
                      <span className="text-xs font-semibold uppercase tracking-wide text-amber-700">
                        Cloud Models — billed to the HOD budget
                      </span>
                    </div>
                    <div className="divide-y divide-gray-100">
                      {cloudModels.map((m) => (
                        <ModelOption
                          key={m.id}
                          model={m}
                          checked={selectedModels.includes(m.id)}
                          onToggle={toggleModel}
                        />
                      ))}
                    </div>
                  </>
                )}
                {localModels.length > 0 && (
                  <>
                    <div className="px-3 py-2 bg-gray-50 border-y border-gray-100">
                      <span className="text-xs font-semibold uppercase tracking-wide text-gray-400">
                        Local Models — in-house, free
                      </span>
                    </div>
                    <div className="divide-y divide-gray-100">
                      {localModels.map((m) => (
                        <ModelOption
                          key={m.id}
                          model={m}
                          checked={selectedModels.includes(m.id)}
                          onToggle={toggleModel}
                        />
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}
            {formErrors.models && (
              <p className="mt-1 text-xs text-red-600">{formErrors.models}</p>
            )}
          </div>

          {/* ── HOD budget owner — required once any cloud model is selected ── */}
          <div ref={hodRef} className="relative">
            <label className="block text-xs text-gray-500 mb-1">
              <span className="flex items-center gap-1">
                <Wallet size={12} className="text-gray-400" />
                HOD Budget Owner
                {cloudSelected && <span className="text-red-500">*</span>}
                <span className="text-gray-400 font-normal">
                  {cloudSelected
                    ? "(funds this endpoint's cloud usage)"
                    : "(optional — only needed for cloud models)"}
                </span>
              </span>
            </label>

            <button
              type="button"
              onClick={() => setHodOpen((v) => !v)}
              className={`w-full flex items-center justify-between px-3 py-2 text-sm border rounded-lg bg-white text-left transition-colors hover:bg-gray-50 ${
                formErrors.hod ? "border-red-400" : "border-gray-200"
              }`}
            >
              <span className={hodEmail ? "text-gray-800" : "text-gray-400"}>
                {hodEmail
                  ? selectedHod
                    ? `${selectedHod.hod_name} — ${hodEmail}`
                    : hodEmail
                  : hodsLoading
                    ? "Loading HODs…"
                    : "Select a HOD…"}
              </span>
              <span className="flex items-center gap-1.5 flex-shrink-0 ml-2">
                {hodEmail && (
                  <X
                    size={14}
                    className="text-gray-400 hover:text-gray-700"
                    onClick={(e) => {
                      e.stopPropagation();
                      setHodEmail("");
                      setFormErrors((prev) => ({ ...prev, hod: "" }));
                    }}
                  />
                )}
                <Search size={13} className="text-gray-400" />
              </span>
            </button>

            {hodOpen && (
              <div className="absolute z-20 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow-lg">
                <div className="p-2 border-b border-gray-100">
                  <input
                    autoFocus
                    value={hodQuery}
                    onChange={(e) => setHodQuery(e.target.value)}
                    placeholder="Search by name, email, or department…"
                    className="w-full px-2.5 py-1.5 text-sm border border-gray-200 rounded focus:outline-none focus:ring-1 focus:ring-indigo-400"
                  />
                </div>
                <div className="max-h-56 overflow-y-auto divide-y divide-gray-100">
                  {filteredHods.length === 0 ? (
                    <p className="px-3 py-3 text-xs text-gray-400">No matching HODs.</p>
                  ) : (
                    filteredHods.map((h) => {
                      const b = h.budget || {};
                      const isSel = h.hod_email === hodEmail;
                      return (
                        <button
                          type="button"
                          key={h.hod_email}
                          onClick={() => {
                            setHodEmail(h.hod_email);
                            setHodOpen(false);
                            setHodQuery("");
                            setFormErrors((prev) => ({ ...prev, hod: "" }));
                          }}
                          className={`w-full text-left px-3 py-2 hover:bg-gray-50 transition-colors ${isSel ? "bg-indigo-50" : ""}`}
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-sm text-gray-800 truncate">
                              {h.hod_name || h.hod_email}
                            </span>
                            {isSel && <Check size={13} className="text-indigo-600 flex-shrink-0" />}
                          </div>
                          <div className="text-xs text-gray-500 truncate">{h.hod_email}</div>
                          {b.cap_usd != null && (
                            <div className="text-xs text-gray-400 mt-0.5">
                              ${Number(b.remaining_usd || 0).toFixed(2)} of $
                              {Number(b.cap_usd || 0).toFixed(2)} remaining
                            </div>
                          )}
                        </button>
                      );
                    })
                  )}
                </div>
              </div>
            )}
            {formErrors.hod && <p className="mt-1 text-xs text-red-600">{formErrors.hod}</p>}

            {/* Live budget banner — shown when cloud models are in play */}
            {cloudSelected && selectedHod?.budget && (
              <BudgetBanner budget={selectedHod.budget} />
            )}
          </div>

          {/* ── Fallback indicator — read-only, computed by the server ──────
              Not a selectable field: the server always picks a local model
              (free) if one is in the allowlist, otherwise the cheapest
              selected cloud model. Shown here purely so the admin can see
              what an unrecognised model would resolve to, recomputed live as
              checkboxes change. */}
          {selectedModels.length > 0 && (
            <div className="text-xs text-gray-500 px-1">
              Fallback for unrecognised models:{" "}
              {fallbackPreview ? (
                <span className="font-medium text-gray-700">
                  {fallbackPreview.model}{" "}
                  {fallbackPreview.kind === "local" ? "(local, free)" : "(cheapest cloud, billed)"}
                </span>
              ) : (
                <span className="italic">none selected</span>
              )}
            </div>
          )}

          {/* use_env_key toggle */}
          <div className="flex items-center justify-between py-3 px-4 bg-gray-50 rounded-lg border border-gray-200">
            <div>
              <p className="text-xs text-gray-500">Use Team-Specific LiteLLM Key</p>
              <p className="text-xs text-gray-500 mt-0.5">
                When ON, the platform forwards a dedicated LiteLLM virtual key for this team.
                When OFF, the global <span className="font-mono">LOCAL_LLM_API_KEY</span> is used.
              </p>
            </div>
            <button type="button" onClick={handleUseEnvKeyToggle} className="flex-shrink-0 ml-4">
              {useEnvKey ? (
                <ToggleRight size={28} className="text-indigo-600" />
              ) : (
                <ToggleLeft size={28} className="text-gray-400" />
              )}
            </button>
          </div>

          {/* tool_calls_enabled toggle */}
          <div className="flex items-center justify-between py-3 px-4 bg-gray-50 rounded-lg border border-gray-200">
            <div>
              <p className="text-xs text-gray-500">Allow Tool Calls</p>
              <p className="text-xs text-gray-500 mt-0.5">
                When ON, callers may pass <span className="font-mono">tools</span> and{" "}
                <span className="font-mono">tool_choice</span> in requests.
                When OFF, requests with tool calls are rejected with a clear error.
              </p>
            </div>
            <button type="button" onClick={() => setToolCallsEnabled((v) => !v)} className="flex-shrink-0 ml-4">
              {toolCallsEnabled ? (
                <ToggleRight size={28} className="text-indigo-600" />
              ) : (
                <ToggleLeft size={28} className="text-gray-400" />
              )}
            </button>
          </div>

          {/* env_key_name — only shown when use_env_key=true */}
          {useEnvKey && (
            <div>
              <label className="block text-xs text-gray-500 mb-1">
                .env Variable Name <span className="text-red-500">*</span>
              </label>
              <div className={`flex items-center bg-white border rounded overflow-hidden focus-within:border-indigo-300 ${
                formErrors.envKeyName ? "border-red-500" : "border-gray-300"
              }`}>
                <span className="px-3 py-2 bg-gray-50 text-gray-400 text-sm border-r border-gray-200 select-none">
                  .env →
                </span>
                <input
                  type="text"
                  value={envKeyName}
                  onChange={(e) => handleEnvKeyChange(e.target.value)}
                  placeholder="TEAM_LITELLM_API_KEY"
                  className="flex-1 px-3 py-2 text-sm font-mono focus:outline-none"
                />
              </div>
              <p className="text-xs text-gray-400 mt-1">
                The LiteLLM virtual key for this team must be stored in{" "}
                <span className="font-mono">.env</span> under this variable name.
              </p>
              {formErrors.envKeyName && <p className="mt-1 text-xs text-red-600">{formErrors.envKeyName}</p>}
            </div>
          )}
        </form>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-gray-100">
          <button
            onClick={handleSubmit}
            disabled={saving}
            className="px-4 py-2 cursor-pointer disabled:opacity-50 rounded text-sm font-medium text-white transition-colors brand-grad hover:opacity-70 flex items-center gap-2"
          >
            {saving && <RefreshCw size={14} className="animate-spin" />}
            {isEdit ? "Save Changes" : "Submit"}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 bg-white border border-gray-300 hover:bg-gray-100 rounded text-sm text-gray-700 transition-colors cursor-pointer"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export default function EndpointManager({ user }) {
  const { isAdmin } = usePermission(user);
  const { toast } = useToast();
  const { confirm } = useConfirm();

  const [endpoints, setEndpoints] = useState([]);
  const [loading, setLoading] = useState(true);

  // Modal / panel state
  const [showCreateEdit, setShowCreateEdit] = useState(false);
  const [editingEndpoint, setEditingEndpoint] = useState(null);
  const [revealKey, setRevealKey] = useState(null);          // raw key string to show once
  const [previewEndpointId, setPreviewEndpointId] = useState(null); // which row's model preview is open

  // ---------------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------------

  const loadEndpoints = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await authFetch(`${API_BASE}/endpoint-mgmt/`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setEndpoints(data.endpoints || []);
    } catch (err) {
      toast.error("Failed to load endpoints: " + err.message);
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    if (isAdmin) loadEndpoints();
  }, [isAdmin, loadEndpoints]);

  // ---------------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------------

  const handleSave = (data) => {
    setShowCreateEdit(false);
    setEditingEndpoint(null);
    if (data.key) setRevealKey(data.key);   // show key reveal modal on create
    loadEndpoints();
  };

  const handleToggle = async (ep) => {
    try {
      const resp = await authFetch(`${API_BASE}/endpoint-mgmt/${ep.id}/toggle`, { method: "PATCH" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      toast.success("Endpoint updated.");
      loadEndpoints();
    } catch (err) {
      toast.error("Failed to toggle endpoint: " + err.message);
    }
  };

  const handleToggleToolCalls = async (ep) => {
    try {
      const resp = await authFetch(`${API_BASE}/endpoint-mgmt/${ep.id}`, {
        method: "PUT",
        headers: JSON_HEADERS,
        body: JSON.stringify({ tool_calls_enabled: !ep.tool_calls_enabled }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      toast.success(`Tool calls ${!ep.tool_calls_enabled ? "enabled" : "disabled"}.`);
      loadEndpoints();
    } catch (err) {
      toast.error("Failed to toggle tool calls: " + err.message);
    }
  };

  const handleDelete = async (ep) => {
    try {
      const resp = await authFetch(`${API_BASE}/endpoint-mgmt/${ep.id}`, { method: "DELETE" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      toast.success("Endpoint deleted.");
      loadEndpoints();
    } catch (err) {
      toast.error("Failed to delete endpoint: " + err.message);
    }
  };

  const handleRegenKey = async (ep) => {
    try {
      const resp = await authFetch(`${API_BASE}/endpoint-mgmt/${ep.id}/regenerate-key`, {
        method: "POST",
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      toast.success("API key regenerated.");
      setRevealKey(data.key);
      loadEndpoints();
    } catch (err) {
      toast.error("Failed to regenerate key: " + err.message);
    }
  };

  // ---------------------------------------------------------------------------
  // Guard
  // ---------------------------------------------------------------------------

  if (!isAdmin) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <Shield size={40} className="text-gray-300 mx-auto mb-3" />
          <p className="text-gray-500 font-medium">Admin access required</p>
          <p className="text-gray-400 text-sm mt-1">Only admins can manage endpoints.</p>
        </div>
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="border-b border-gray-200 p-4 flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-3">
          <Globe size={18} className="text-indigo-600" />
          <div>
            <h1 className="text-sm font-semibold text-indigo-700">Endpoint Management</h1>
            <p className="text-xs text-gray-400">
              Manage named OpenAI-compatible proxy endpoints with platform-generated API keys.
            </p>
          </div>
        </div>
        <button
          onClick={() => { setEditingEndpoint(null); setShowCreateEdit(true); }}
          className="flex items-center gap-1.5 px-3 py-1.5 brand-grad hover:opacity-70 text-white text-xs rounded cursor-pointer"
        >
          <PlusCircle size={13} />
          New Endpoint
        </button>
      </div>

      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto p-6">

      {/* Info box */}
      <div className="mb-6 bg-gray-50 border border-gray-200 rounded-lg px-4 py-3 flex items-start gap-3">
        <Key size={16} className="text-gray-400 flex-shrink-0 mt-0.5" />
        <div className="text-xs text-gray-500 space-y-1">
          <p>
            <strong className="text-gray-700">How it works:</strong> Each endpoint gets a platform-generated API key (stored
            securely alongside CLI keys). Teams call{" "}
            <span className="font-mono bg-white border border-gray-200 px-1 rounded text-gray-600">
              POST /ainxt/v1/api/&#123;slug&#125;/v1/chat/completions
            </span>{" "}
            with{" "}
            <span className="font-mono bg-white border border-gray-200 px-1 rounded text-gray-600">Authorization: Bearer &lt;key&gt;</span>.
          </p>
          <p>
            <strong className="text-gray-700">LiteLLM key:</strong> Toggle "Use Team-Specific LiteLLM Key" to forward a
            dedicated virtual key to LiteLLM (enabling per-team model restrictions and budgets).
            When OFF, the global <span className="font-mono bg-white border border-gray-200 px-1 rounded text-gray-600">LOCAL_LLM_API_KEY</span> is used.
          </p>
        </div>
      </div>

      {/* Table */}
      <div className="border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Endpoint</th>
              <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Platform Key</th>
              <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">LiteLLM Mode</th>
              <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Models</th>
              <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Tool Calls</th>
              <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Status</th>
              <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {loading ? (
              <tr>
                <td colSpan={7} className="text-center py-12 text-gray-400">
                  <RefreshCw size={20} className="animate-spin mx-auto mb-2" />
                  Loading endpoints…
                </td>
              </tr>
            ) : endpoints.length === 0 ? (
              <tr>
                <td colSpan={7} className="text-center py-16">
                  <Globe size={36} className="text-gray-200 mx-auto mb-3" />
                  <p className="text-gray-500 font-medium">No endpoints yet</p>
                  <p className="text-gray-400 text-xs mt-1">Click "New Endpoint" to get started.</p>
                </td>
              </tr>
            ) : (
              endpoints.map((ep) => (
                <>
                  <tr key={ep.id} className="hover:bg-gray-50 transition-colors">
                    {/* Endpoint */}
                    <td className="px-4 py-3">
                      <div className="font-medium text-gray-900 truncate whitespace-nowrap max-w-[140px]" title={ep.name}>
                        {ep.name}
                      </div>
                      <div className="font-mono text-xs text-indigo-600 mt-0.5 truncate whitespace-nowrap max-w-[140px]" title={ep.slug}>
                        /{ep.slug}
                      </div>
                      {ep.description && (
                        <div className="text-xs text-gray-400 mt-0.5 max-w-[140px] truncate whitespace-nowrap" title={ep.description}>
                          {truncate(ep.description, 40)}
                        </div>
                      )}
                    </td>

                    {/* Platform Key */}
                    <td className="px-4 py-3 whitespace-nowrap">
                      {ep.key_prefix ? (
                        <span className="font-mono text-xs bg-gray-100 text-gray-700 px-2 py-0.5 rounded border border-gray-200 inline-block whitespace-nowrap">
                          {ep.key_prefix}••••••
                        </span>
                      ) : (
                        <span className="text-xs text-gray-400 italic whitespace-nowrap">No key</span>
                      )}
                    </td>

                    {/* LiteLLM Mode */}
                    <td className="px-4 py-3">
                      {ep.use_env_key ? (
                        <div className="flex flex-col gap-1">
                          <span className="inline-flex items-center gap-1 text-xs font-medium text-indigo-700 bg-indigo-50 border border-indigo-200 px-2 py-0.5 rounded-full w-fit">
                            Env Key
                          </span>
                          <span className="font-mono text-xs text-gray-500">{ep.env_key_name}</span>
                          {!ep.env_key_configured && (
                            <span className="inline-flex items-center gap-1 text-xs text-red-600">
                              <AlertTriangle size={11} /> Not set in .env
                            </span>
                          )}
                        </div>
                      ) : (
                        <span className="text-xs text-gray-500 whitespace-nowrap">Global Key</span>
                      )}
                    </td>

                    {/* Models — eye icon + count; click to expand preview panel.
                        A cloud badge makes billable endpoints obvious at a glance. */}
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => setPreviewEndpointId(previewEndpointId === ep.id ? null : ep.id)}
                          className="flex items-center gap-1.5 group cursor-pointer"
                          title="Preview allowed models"
                        >
                          <Eye size={14} className="text-gray-400 group-hover:text-indigo-600 transition-colors" />
                          <span className="text-xs text-gray-500 group-hover:text-indigo-600 transition-colors">Preview</span>
                        </button>
                        {ep.cloud_enabled && (
                          <span
                            title={
                              ep.hod_email
                                ? `${(ep.cloud_models || []).length} cloud model(s) · funded by ${ep.hod_email}`
                                : "Cloud models enabled but NO budget owner — cloud requests will be refused"
                            }
                            className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-xs font-medium border ${
                              ep.hod_email
                                ? "bg-amber-50 text-amber-800 border-amber-200"
                                : "bg-red-50 text-red-700 border-red-200"
                            }`}
                          >
                            <Cloud size={10} />
                            {(ep.cloud_models || []).length}
                            {!ep.hod_email && <AlertTriangle size={10} />}
                          </span>
                        )}
                      </div>
                    </td>

                    {/* Tool Calls */}
                    <td className="px-4 py-3">
                      <button
                        onClick={() => handleToggleToolCalls(ep)}
                        className="flex items-center gap-1.5 group cursor-pointer"
                        title={ep.tool_calls_enabled ? "Click to disable tool calls" : "Click to enable tool calls"}
                      >
                        {ep.tool_calls_enabled ? (
                          <>
                            <span className="w-2 h-2 rounded-full bg-green-500 flex-shrink-0" />
                            <span className="text-xs font-medium text-green-700 group-hover:text-green-900">Enabled</span>
                            <ToggleRight size={16} className="text-green-500 group-hover:text-green-700" />
                          </>
                        ) : (
                          <>
                            <span className="w-2 h-2 rounded-full bg-gray-300 flex-shrink-0" />
                            <span className="text-xs font-medium text-gray-400 group-hover:text-gray-600">Disabled</span>
                            <ToggleLeft size={16} className="text-gray-300 group-hover:text-gray-500" />
                          </>
                        )}
                      </button>
                    </td>

                    {/* Status */}
                    <td className="px-4 py-3">
                      <button
                        onClick={() => handleToggle(ep)}
                        className="flex items-center gap-1.5 group cursor-pointer"
                        title={ep.enabled ? "Click to disable" : "Click to enable"}
                      >
                        {ep.enabled ? (
                          <>
                            <span className="w-2 h-2 rounded-full bg-green-500 flex-shrink-0" />
                            <span className="text-xs font-medium text-green-700 group-hover:text-green-900">Active</span>
                            <ToggleRight size={16} className="text-green-500 group-hover:text-green-700" />
                          </>
                        ) : (
                          <>
                            <span className="w-2 h-2 rounded-full bg-gray-300 flex-shrink-0" />
                            <span className="text-xs font-medium text-gray-400 group-hover:text-gray-600">Disabled</span>
                            <ToggleLeft size={16} className="text-gray-300 group-hover:text-gray-500" />
                          </>
                        )}
                      </button>
                    </td>

                    {/* Actions */}
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-1">
                        <button
                          onClick={() => { setEditingEndpoint(ep); setShowCreateEdit(true); }}
                          title="Edit endpoint"
                          className="p-1.5 rounded-lg text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors cursor-pointer"
                        >
                          <Pencil size={15} />
                        </button>
                        <button
                          onClick={async () => {
                            const ok = await confirm({
                              title: "Regenerate API Key",
                              message: `Generate a new API key for "${ep.name}"? The current key will stop working immediately. You'll need to share the new key with your team.`,
                              confirmLabel: "Regenerate",
                              variant: "danger",
                            });
                            if (ok) handleRegenKey(ep);
                          }}
                          title="Regenerate API key"
                          className="p-1.5 rounded-lg text-gray-400 hover:text-amber-600 hover:bg-amber-50 transition-colors cursor-pointer"
                        >
                          <Key size={15} />
                        </button>
                        <button
                          onClick={async () => {
                            const ok = await confirm({
                              title: "Delete Endpoint",
                              message: `Delete "${ep.name}"? The API key will be revoked and all requests to /${ep.slug} will immediately return 404.`,
                              confirmLabel: "Delete",
                              variant: "danger",
                            });
                            if (ok) handleDelete(ep);
                          }}
                          title="Delete endpoint"
                          className="p-1.5 rounded-lg text-gray-400 hover:text-red-600 hover:bg-red-50 transition-colors cursor-pointer"
                        >
                          <Trash2 size={15} />
                        </button>
                      </div>
                    </td>
                  </tr>

                  {/* Models preview panel — expands below the row when eye icon clicked */}
                  {previewEndpointId === ep.id && (
                    <tr key={`${ep.id}-preview`}>
                      <td colSpan={7} className="px-4 pb-3">
                        <ModelsPreviewPanel
                          models={ep.model_ids || []}
                          cloudModels={ep.cloud_models || []}
                          onClose={() => setPreviewEndpointId(null)}
                        />
                      </td>
                    </tr>
                  )}
                </>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Count */}
      {!loading && endpoints.length > 0 && (
        <p className="text-xs text-gray-400 mt-3 text-right">
          {endpoints.length} endpoint{endpoints.length !== 1 ? "s" : ""}
        </p>
      )}

      {/* Missing env key warning */}
      {!loading && endpoints.some((ep) => ep.use_env_key && !ep.env_key_configured) && (
        <div className="mt-4 bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 flex items-start gap-3">
          <AlertTriangle size={16} className="text-amber-500 flex-shrink-0 mt-0.5" />
          <div className="text-sm text-amber-800">
            <strong>Action required:</strong> One or more endpoints have "Use Env Key" enabled but
            the corresponding <span className="font-mono">.env</span> variable is not set.
            Add the key and restart the gateway.
          </div>
        </div>
      )}

      {/* ── Modals ── */}

      {showCreateEdit && (
        <EndpointModal
          endpoint={editingEndpoint}
          onSave={handleSave}
          onClose={() => { setShowCreateEdit(false); setEditingEndpoint(null); }}
        />
      )}

      {revealKey && (
        <KeyRevealModal rawKey={revealKey} onClose={() => setRevealKey(null)} />
      )}

      </div>{/* end scrollable body */}
    </div>
  );
}