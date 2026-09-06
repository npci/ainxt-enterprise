// SPDX-License-Identifier: MIT
// BuildMetadataApprovalPanel — rendered when run.state === "AWAITING_BUILD_METADATA_APPROVAL".
//
// The pipeline detected a language version on the BASE BRANCH checkout that
// differs from the stored (product, repo) build metadata. The operator confirms
// which version to use; the backend persists it to repo_build_metadata
// (product, repo), refreshes the resolved-manifest cache, and resumes the
// pipeline at BASELINE.
//
// Expected shape of `gate` (run.context.build_metadata_gate):
//   { repo, product_id, detected_version, stored_version, build_tool, language }

import { useState } from "react";
import { PackageCheck } from "lucide-react";
import { API_BASE as API, apiFetch } from "../config";

export default function BuildMetadataApprovalPanel({ runId, gate, onSubmitted }) {
  const g = gate || {};
  const detected = String(g.detected_version || "").trim();
  const stored = String(g.stored_version || "").trim();

  // Default to the base-branch-detected version — the build files are the
  // source of truth in the common case.
  const [choice, setChoice] = useState("detected");
  const [customVersion, setCustomVersion] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  if (!detected && !stored) return null;

  const canSubmit = () =>
    choice === "detected" || choice === "stored" ||
    (choice === "custom" && customVersion.trim().length > 0);

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    const payload = {
      choice,
      chosen_version: choice === "custom" ? customVersion.trim() : "",
    };
    try {
      const r = await apiFetch(`${API}/sdlc/runs/${runId}/build-metadata/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const text = await r.text();
        let detail = text;
        try {
          detail = JSON.parse(text).detail || text;
        } catch {
          /* use raw */
        }
        throw new Error(detail || `HTTP ${r.status}`);
      }
      if (onSubmitted) onSubmitted();
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const langLabel = [g.language, g.build_tool].filter(Boolean).join(" · ");

  const OptionRow = ({ value, title, version, hint }) => (
    <label className="flex items-start gap-2 cursor-pointer hover:bg-yellow-50 rounded px-1 py-1">
      <input
        type="radio"
        name="build-metadata-version"
        checked={choice === value}
        onChange={() => setChoice(value)}
        className="mt-0.5 flex-shrink-0"
      />
      <span className="text-xs text-gray-700 flex-1">
        <span className="font-medium">{title}</span>
        {version ? (
          <code className="ml-1.5 font-mono text-indigo-600">{version}</code>
        ) : null}
        {hint ? <span className="ml-1.5 text-[11px] text-gray-500">{hint}</span> : null}
      </span>
    </label>
  );

  return (
    <div className="mb-3 space-y-3">
      <div className="flex items-center gap-2">
        <PackageCheck size={14} className="text-yellow-600" />
        <span className="text-xs font-semibold text-yellow-800">
          Confirm build version{g.repo ? ` — ${g.repo}` : ""}
        </span>
      </div>
      <p className="text-xs text-gray-600 italic">
        The {langLabel || "build"} version detected on the base branch does not match the
        stored build metadata for this product/repo. Confirm which version the pipeline
        should build with — your choice is saved for this product + repo.
      </p>

      <div className="border border-yellow-200 rounded p-3 bg-white space-y-1.5">
        <OptionRow
          value="detected"
          title="Use detected (base branch)"
          version={detected || "—"}
          hint="from pom.xml / build.gradle / etc."
        />
        <OptionRow
          value="stored"
          title="Keep stored"
          version={stored || "—"}
          hint="previously recorded"
        />
        <OptionRow value="custom" title="Other version:" />
        {choice === "custom" && (
          <input
            type="text"
            className="w-full border border-yellow-300 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-yellow-400"
            placeholder="e.g. 17"
            value={customVersion}
            onChange={(e) => setCustomVersion(e.target.value)}
          />
        )}
      </div>

      {error && (
        <div className="px-3 py-1.5 bg-red-50 border border-red-200 rounded text-xs text-red-700">
          {error}
        </div>
      )}

      <button
        type="button"
        onClick={handleSubmit}
        disabled={!canSubmit() || submitting}
        className="w-full px-3 py-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-gray-300 disabled:cursor-not-allowed text-white text-xs font-medium rounded transition-colors"
      >
        {submitting ? "Submitting..." : "Confirm Version & Resume Pipeline"}
      </button>
    </div>
  );
}
