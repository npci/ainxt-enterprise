// SPDX-License-Identifier: MIT
// WorkItemPanel — GATE 1 (gate_kind==="normalization"). Displayed at
// AWAITING_USER_INPUT so the human reviews/edits the NormalizationAgent's
// WorkItem BEFORE any CLASSIFY/PLAN CLI spend happens. Fires unconditionally,
// even when there are zero pending_questions — Approve always posts to
// /sdlc/runs/{runId}/answer-questions with `work_item` (edited scope/
// out_of_scope/acceptance_criteria) + `answers` (aligned with pending_questions,
// [] when there are none). Any pending_questions raised by the normalizer are
// answered inline in THIS panel, not via <OpenQuestionsForm/> (that component is
// GATE 2 only — classify-stage questions).
import { useState } from "react";
import { ChevronDown, ChevronRight, AlertTriangle, Sparkles, Plus, X } from "lucide-react";
import { API_BASE as API, apiFetch } from "../config";

function ReadOnlySection({ title, items, isOos }) {
  const [open, setOpen] = useState(true);
  if (!items || items.length === 0) return null;
  return (
    <div className={`mb-3 ${isOos ? 'bg-red-50 border border-red-200 rounded-lg p-2' : ''}`}>
      <button
        type="button"
        className="flex items-center gap-1 text-sm font-semibold text-gray-700 mb-1"
        onClick={() => setOpen(o => !o)}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {isOos && <AlertTriangle size={14} className="text-red-500" />}
        {title}
      </button>
      {open && (
        <ul className="space-y-1 pl-4">
          {items.map((item, i) => (
            <li key={i} className={`text-sm ${isOos ? 'text-red-700 font-medium' : 'text-gray-600'}`}>
              {item.includes('/') || item.includes('.') ? (
                <code className={`px-1 rounded text-xs ${isOos ? 'bg-red-100' : 'bg-gray-100'}`}>{item}</code>
              ) : item}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Editable list — used for scope / out_of_scope / acceptance_criteria. Simple
// add/remove/edit; no drag-reorder, no rich text — per-item inline text input.
function EditableList({ title, items, onChange, isOos }) {
  const [draft, setDraft] = useState("");

  const addItem = () => {
    const v = draft.trim();
    if (!v) return;
    onChange([...(items || []), v]);
    setDraft("");
  };
  const removeItem = (i) => onChange((items || []).filter((_, idx) => idx !== i));
  const updateItem = (i, val) => onChange((items || []).map((it, idx) => (idx === i ? val : it)));

  return (
    <div className={`mb-3 ${isOos ? 'bg-red-50 border border-red-200 rounded-lg p-2' : ''}`}>
      <p className={`flex items-center gap-1 text-sm font-semibold mb-1.5 ${isOos ? 'text-red-700' : 'text-gray-700'}`}>
        {isOos && <AlertTriangle size={14} className="text-red-500" />}
        {title}
        {isOos && <span className="text-red-600 text-xs font-normal ml-1">— do not touch</span>}
      </p>
      <ul className="space-y-1.5">
        {(items || []).map((item, i) => (
          <li key={i} className="flex items-center gap-1.5">
            <input
              value={item}
              onChange={e => updateItem(i, e.target.value)}
              className="flex-1 text-sm border border-gray-200 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-indigo-400"
            />
            <button
              type="button"
              onClick={() => removeItem(i)}
              className="text-gray-400 hover:text-red-600 flex-shrink-0"
            >
              <X size={14} />
            </button>
          </li>
        ))}
      </ul>
      <div className="flex items-center gap-1.5 mt-1.5">
        <input
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); addItem(); } }}
          placeholder={`Add to ${title.toLowerCase()}...`}
          className="flex-1 text-sm border border-dashed border-gray-300 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-indigo-400"
        />
        <button
          type="button"
          onClick={addItem}
          className="text-indigo-600 hover:text-indigo-800 flex-shrink-0"
        >
          <Plus size={16} />
        </button>
      </div>
    </div>
  );
}

// Inline question card — same shape as OpenQuestionsForm's question rendering,
// but embedded directly in this panel since GATE 1 answers ride the SAME
// answer-questions POST as the work_item edits (one submit, one request).
function InlineQuestion({ q, idx, answer, onChange }) {
  const hasOptions = Array.isArray(q.options) && q.options.length > 0;
  const recIdx = typeof q.recommended === "number" ? q.recommended : null;

  return (
    <div className="border border-yellow-200 rounded p-3 bg-white space-y-2">
      <div className="text-xs font-medium text-gray-800">{idx + 1}. {q.question}</div>
      {q.rationale && (
        <p className="text-[11px] text-gray-500 italic leading-relaxed">{q.rationale}</p>
      )}
      <div className="space-y-1.5 mt-1">
        {hasOptions && q.options.map((opt, oi) => (
          <label key={oi} className="flex items-start gap-2 cursor-pointer hover:bg-yellow-50 rounded px-1 py-0.5">
            <input
              type="radio"
              name={`wi-q-${idx}`}
              checked={!answer.useOther && answer.selectedOption === oi}
              onChange={() => onChange({ selectedOption: oi, useOther: false, freeText: "" })}
              className="mt-0.5 flex-shrink-0"
            />
            <span className="text-xs text-gray-700 flex-1">
              {opt}
              {recIdx === oi && (
                <span className="ml-1.5 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 text-[10px] font-medium">
                  <Sparkles size={9} /> Recommended
                </span>
              )}
            </span>
          </label>
        ))}
        {hasOptions && (
          <label className="flex items-start gap-2 cursor-pointer hover:bg-yellow-50 rounded px-1 py-0.5">
            <input
              type="radio"
              name={`wi-q-${idx}`}
              checked={answer.useOther}
              onChange={() => onChange({ useOther: true })}
              className="mt-0.5 flex-shrink-0"
            />
            <span className="text-xs text-gray-700 flex-1">Other &mdash; pick a different direction:</span>
          </label>
        )}
        {(answer.useOther || !hasOptions) && (
          <textarea
            className="w-full border border-yellow-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-yellow-400"
            rows={2}
            placeholder="Your answer..."
            value={answer.freeText}
            onChange={e => onChange({ freeText: e.target.value })}
          />
        )}
      </div>
    </div>
  );
}

export default function WorkItemPanel({ runId, workItem, questions, onSubmitted }) {
  const wi = workItem || {};
  const qs = questions || [];

  const [scope, setScope] = useState(wi.scope || []);
  const [outOfScope, setOutOfScope] = useState(wi.out_of_scope || []);
  const [acceptanceCriteria, setAcceptanceCriteria] = useState(wi.acceptance_criteria || []);
  const [answers, setAnswers] = useState(() =>
    qs.map(q => {
      const hasOptions = Array.isArray(q.options) && q.options.length > 0;
      return {
        selectedOption: hasOptions && typeof q.recommended === "number" ? q.recommended : null,
        freeText: "",
        useOther: !hasOptions,
      };
    })
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  if (!wi.problem_statement && !wi.jira_key) return null;

  const updateAnswer = (idx, patch) => {
    setAnswers(prev => prev.map((a, i) => (i === idx ? { ...a, ...patch } : a)));
  };

  const canSubmit = () =>
    answers.every(a => (a.useOther ? a.freeText.trim().length > 0 : typeof a.selectedOption === "number"));

  const handleApprove = async () => {
    setSubmitting(true);
    setError(null);
    const payload = {
      answers: qs.map((q, i) => ({
        selected_option: answers[i].useOther ? null : answers[i].selectedOption,
        answer: answers[i].useOther
          ? answers[i].freeText.trim()
          : typeof answers[i].selectedOption === "number"
          ? q.options[answers[i].selectedOption]
          : "",
      })),
      work_item: {
        scope,
        out_of_scope: outOfScope,
        acceptance_criteria: acceptanceCriteria,
      },
    };
    try {
      const r = await apiFetch(`${API}/sdlc/runs/${runId}/answer-questions`, {
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

  return (
    <div className="bg-white border border-sky-200 rounded-xl p-4 shadow-sm">
      <div className="flex items-center gap-2 mb-3">
        <span className="text-lg">📋</span>
        <h3 className="font-semibold text-gray-800">Work Item — Confirm Understanding</h3>
        {wi.jira_key && (
          <span className="text-xs bg-sky-100 text-sky-700 px-2 py-0.5 rounded-full">
            {wi.jira_key}
          </span>
        )}
      </div>

      {wi.problem_statement && (
        <div className="mb-3 bg-gray-50 rounded-lg p-3">
          <p className="text-xs font-semibold text-gray-500 mb-1">PROBLEM STATEMENT</p>
          <p className="text-sm text-gray-700">{wi.problem_statement}</p>
        </div>
      )}

      <EditableList title="Acceptance Criteria" items={acceptanceCriteria} onChange={setAcceptanceCriteria} />
      <EditableList title="In Scope" items={scope} onChange={setScope} />
      <EditableList title="Out of Scope" items={outOfScope} onChange={setOutOfScope} isOos />

      <ReadOnlySection title="Constraints" items={wi.constraints} />
      <ReadOnlySection title="Technical Hints" items={wi.technical_hints} />

      {qs.length > 0 && (
        <div className="mt-3 border-t border-gray-100 pt-3 space-y-2">
          <p className="text-xs font-semibold text-gray-500 mb-1">OPEN QUESTIONS</p>
          {qs.map((q, i) => (
            <InlineQuestion
              key={i}
              q={q}
              idx={i}
              answer={answers[i]}
              onChange={patch => updateAnswer(i, patch)}
            />
          ))}
        </div>
      )}

      {error && (
        <div className="mt-3 px-3 py-1.5 bg-red-50 border border-red-200 rounded text-xs text-red-700">
          {error}
        </div>
      )}

      <div className="mt-3 flex gap-2 border-t border-gray-100 pt-3">
        <button
          type="button"
          disabled={!canSubmit() || submitting}
          className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
          onClick={handleApprove}
        >
          {submitting ? "Submitting..." : "Approve & Continue"}
        </button>
      </div>
    </div>
  );
}
