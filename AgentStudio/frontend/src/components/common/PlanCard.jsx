// SPDX-License-Identifier: MIT
import { useMemo, useState } from 'react';

/**
 * PlanCard — structured pre-generation questionnaire shown on turn 1 of the
 * Agent / Workflow / Skill factories.
 *
 * The backend emits an SSE event `{ stage: "plan_card", data: { plan_card } }`
 * where `plan_card = { questions: [{ id, label, default, options, multi_select?,
 * allow_freetext? }] }`. When the user accepts, we send the answers back as a
 * chat message prefixed `__plan_card__:{json}` (handled by the parent chat).
 *
 * Props:
 *   planCard          : { questions: [...] }
 *   onAccept(answers) : Record<string, string | string[]> → sends the answers
 *   onChangeSomething(): () => void → dismiss + fall through to conversational path
 *   disabled          : boolean (true while streaming)
 */
export default function PlanCard({ planCard, onAccept, onChangeSomething, disabled = false }) {
    const questions = planCard?.questions || [];

    const initial = useMemo(() => {
        const seed = {};
        for (const q of questions) {
            seed[q.id] = q.multi_select
                ? (q.default && q.default !== q.options?.[0] ? [q.default] : [])
                : (q.default ?? q.options?.[0] ?? '');
        }
        return seed;
    }, [planCard]); // eslint-disable-line react-hooks/exhaustive-deps

    const [answers, setAnswers] = useState(initial);
    const [freeText, setFreeText] = useState({}); // { [qid]: draft }

    if (questions.length === 0) return null;

    const isSelected = (q, opt) => {
        const v = answers[q.id];
        return q.multi_select ? Array.isArray(v) && v.includes(opt) : v === opt;
    };

    const pick = (q, opt) => {
        if (disabled) return;
        setAnswers((prev) => {
            if (q.multi_select) {
                const cur = Array.isArray(prev[q.id]) ? prev[q.id] : [];
                const next = cur.includes(opt) ? cur.filter((x) => x !== opt) : [...cur, opt];
                return { ...prev, [q.id]: next };
            }
            return { ...prev, [q.id]: opt };
        });
    };

    const addFreeText = (q) => {
        const draft = (freeText[q.id] || '').trim();
        if (!draft) return;
        setAnswers((prev) => {
            if (q.multi_select) {
                const cur = Array.isArray(prev[q.id]) ? prev[q.id] : [];
                return cur.includes(draft) ? prev : { ...prev, [q.id]: [...cur, draft] };
            }
            return { ...prev, [q.id]: draft };
        });
        setFreeText((prev) => ({ ...prev, [q.id]: '' }));
    };

    const handleAccept = () => {
        if (disabled) return;
        onAccept?.(answers);
    };

    // Custom (free-text) values not present in the option list, so they still
    // render as selected chips.
    const customValues = (q) => {
        const opts = q.options || [];
        const v = answers[q.id];
        if (q.multi_select) return (Array.isArray(v) ? v : []).filter((x) => !opts.includes(x));
        return v && !opts.includes(v) ? [v] : [];
    };

    return (
        <div style={S.card}>
            <div style={S.header}>
                <span style={S.headerIcon} aria-hidden="true">📋</span>
                <span style={S.headerText}>Confirm a few things before I build</span>
            </div>

            {questions.map((q) => (
                <div key={q.id} style={S.question}>
                    <label style={S.label}>{q.label}</label>
                    <div style={S.chips}>
                        {(q.options || []).map((opt) => (
                            <button
                                key={opt}
                                type="button"
                                style={isSelected(q, opt) ? S.chipSel : S.chip}
                                onClick={() => pick(q, opt)}
                                disabled={disabled}
                            >
                                {isSelected(q, opt) ? '✓ ' : ''}{opt}
                            </button>
                        ))}
                        {customValues(q).map((opt) => (
                            <button
                                key={`custom-${opt}`}
                                type="button"
                                style={S.chipSel}
                                onClick={() => pick(q, opt)}
                                disabled={disabled}
                            >
                                ✓ {opt}
                            </button>
                        ))}
                    </div>
                    {q.allow_freetext && (
                        <div style={S.freeRow}>
                            <input
                                type="text"
                                value={freeText[q.id] || ''}
                                onChange={(e) => setFreeText((p) => ({ ...p, [q.id]: e.target.value }))}
                                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addFreeText(q); } }}
                                placeholder="Add your own…"
                                style={S.freeInput}
                                disabled={disabled}
                            />
                            <button type="button" style={S.freeAdd} onClick={() => addFreeText(q)} disabled={disabled}>
                                + Add
                            </button>
                        </div>
                    )}
                </div>
            ))}

            <div style={S.actions}>
                <button type="button" style={S.primary(disabled)} onClick={handleAccept} disabled={disabled}>
                    Generate with these settings
                </button>
                {onChangeSomething && (
                    <button type="button" style={S.secondary} onClick={() => !disabled && onChangeSomething()} disabled={disabled}>
                        Change something
                    </button>
                )}
            </div>
        </div>
    );
}

const S = {
    card: {
        display: 'flex', flexDirection: 'column', gap: '14px',
        padding: '16px 18px', margin: '4px 0 2px',
        background: '#ffffff', border: '1px solid #dbe2ea', borderRadius: '14px',
        boxShadow: '0 1px 3px rgba(15,23,42,0.05)',
    },
    header: { display: 'flex', alignItems: 'center', gap: '8px' },
    headerIcon: { fontSize: '15px' },
    headerText: { fontSize: '13px', fontWeight: 700, color: '#0f172a', letterSpacing: '-0.01em' },
    question: { display: 'flex', flexDirection: 'column', gap: '7px' },
    label: { fontSize: '12.5px', fontWeight: 600, color: '#334155' },
    chips: { display: 'flex', flexWrap: 'wrap', gap: '6px' },
    chip: {
        padding: '6px 12px', borderRadius: '999px', border: '1px solid #e2e8f0',
        background: '#f8fafc', color: '#475569', fontSize: '12px', fontWeight: 500,
        cursor: 'pointer', transition: 'all 0.15s',
    },
    chipSel: {
        padding: '6px 12px', borderRadius: '999px', border: '1px solid #4f46e5',
        background: '#4f46e5', color: '#ffffff', fontSize: '12px', fontWeight: 600,
        cursor: 'pointer', transition: 'all 0.15s', boxShadow: '0 2px 6px rgba(99,102,241,0.28)',
    },
    freeRow: { display: 'flex', gap: '6px', marginTop: '2px' },
    freeInput: {
        flex: 1, minWidth: 0, padding: '6px 10px', borderRadius: '8px',
        border: '1px solid #e2e8f0', background: '#fff', fontSize: '12px', color: '#0f172a', outline: 'none',
    },
    freeAdd: {
        padding: '6px 12px', borderRadius: '8px', border: '1px dashed #cbd5e1',
        background: 'transparent', color: '#475569', fontSize: '12px', fontWeight: 550, cursor: 'pointer',
    },
    actions: { display: 'flex', flexWrap: 'wrap', gap: '8px', marginTop: '2px' },
    primary: (disabled) => ({
        flex: '1 1 auto', padding: '9px 18px', borderRadius: '10px', border: 'none',
        background: disabled ? '#c7d2fe' : 'linear-gradient(135deg, #4f46e5, #7c3aed)',
        color: '#fff', fontSize: '13px', fontWeight: 600,
        cursor: disabled ? 'not-allowed' : 'pointer',
        boxShadow: disabled ? 'none' : '0 4px 12px rgba(99,102,241,0.30)',
    }),
    secondary: {
        padding: '9px 16px', borderRadius: '10px', border: '1px solid #e2e8f0',
        background: '#fff', color: '#475569', fontSize: '13px', fontWeight: 550, cursor: 'pointer',
    },
};
