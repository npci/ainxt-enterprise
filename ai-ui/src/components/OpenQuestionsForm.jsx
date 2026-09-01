// SPDX-License-Identifier: Apache-2.0
// OpenQuestionsForm — rendered when run.state === "AWAITING_USER_INPUT".
//
// The analyst raised clarifying questions that JIRA + code did not answer.
// The user picks an option (recommended is pre-selected) or types a free-text
// override. On submit, the backend resumes the pipeline through CLASSIFYING
// → ANALYZING (this time with user_answers injected, so no gate fires) and
// continues to DESIGNING.
//
// Expected shape of `questions` (run.context.pending_questions):
//   [{ id, question, options: [str], recommended: int|null, rationale: str }]

import { useState } from "react";
import { HelpCircle, Sparkles } from "lucide-react";
import { API_BASE as API, apiFetch } from "../config";

export default function OpenQuestionsForm({ runId, questions, onSubmitted }) {
  const [answers, setAnswers] = useState(() =>
    (questions || []).map(q => {
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

  if (!questions || questions.length === 0) return null;

  const updateAnswer = (idx, patch) => {
    setAnswers(prev => prev.map((a, i) => (i === idx ? { ...a, ...patch } : a)));
  };

  const canSubmit = () =>
    answers.every(a => {
      if (a.useOther) return a.freeText.trim().length > 0;
      return typeof a.selectedOption === "number";
    });

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    const payload = {
      answers: answers.map((a, i) => ({
        selected_option: a.useOther ? null : a.selectedOption,
        answer: a.useOther
          ? a.freeText.trim()
          : typeof a.selectedOption === "number"
          ? questions[i].options[a.selectedOption]
          : "",
      })),
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
    <div className="mb-3 space-y-3">
      <div className="flex items-center gap-2">
        <HelpCircle size={14} className="text-yellow-600" />
        <span className="text-xs font-semibold text-yellow-800">
          {questions.length} question{questions.length === 1 ? "" : "s"} from the analyst
        </span>
      </div>
      <p className="text-xs text-gray-600 italic">
        These could not be inferred from the JIRA description alone. Your answers feed directly
        into the design step.
      </p>

      {questions.map((q, i) => {
        const recIdx = typeof q.recommended === "number" ? q.recommended : null;
        const hasOptions = Array.isArray(q.options) && q.options.length > 0;
        const isOtherSelected = answers[i].useOther;
        return (
          <div
            key={q.id || i}
            className="border border-yellow-200 rounded p-3 bg-white space-y-2"
          >
            <div className="text-xs font-medium text-gray-800">
              {i + 1}. {q.question}
            </div>
            {q.rationale && (
              <p className="text-[11px] text-gray-500 italic leading-relaxed">{q.rationale}</p>
            )}

            <div className="space-y-1.5 mt-1">
              {hasOptions && (q.options || []).map((opt, oi) => (
                <label
                  key={oi}
                  className="flex items-start gap-2 cursor-pointer hover:bg-yellow-50 rounded px-1 py-0.5"
                >
                  <input
                    type="radio"
                    name={`q-${i}`}
                    checked={!isOtherSelected && answers[i].selectedOption === oi}
                    onChange={() =>
                      updateAnswer(i, { selectedOption: oi, useOther: false, freeText: "" })
                    }
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
                    name={`q-${i}`}
                    checked={isOtherSelected}
                    onChange={() => updateAnswer(i, { useOther: true })}
                    className="mt-0.5 flex-shrink-0"
                  />
                  <span className="text-xs text-gray-700 flex-1">
                    Other &mdash; pick a different direction:
                  </span>
                </label>
              )}
              {isOtherSelected && (
                <textarea
                  className="w-full border border-yellow-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-yellow-400"
                  rows={2}
                  placeholder={hasOptions ? "Type your answer here..." : "Your answer..."}
                  value={answers[i].freeText}
                  onChange={e => updateAnswer(i, { freeText: e.target.value })}
                />
              )}
            </div>
          </div>
        );
      })}

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
        {submitting ? "Submitting..." : "Submit Answers & Resume Pipeline"}
      </button>
    </div>
  );
}
