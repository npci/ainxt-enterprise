// SPDX-License-Identifier: MIT
import {
    FIELDS,
    getFieldType,
    getOperatorsForType,
    getDefaultValue,
    DEFAULT_OPERATOR,
} from '../../../../constants/operators';

/**
 * LoopConditionRow — single-line condition row for the LoopWhileEditor.
 *
 * Distinct from `SingleCondition` (which the Conditional node uses) on purpose:
 *   - The Conditional row is two-line (field+suggest above, type+op+value below)
 *     because Conditional cases allow free-text custom fields.
 *   - The Loop row is single-line (field select + operator select + value input)
 *     because loops are typically driven by the small set of canonical fields
 *     produced by upstream agents (intent/score/priority/etc). Showing the
 *     manual type picker creates noise for a use-case that never needs it —
 *     the type is fully derivable from the picked field via `getFieldType`.
 *
 * Emits the same condition shape `build_expression_from_case` consumes, so
 * the backend `_run_loop` evaluator is unchanged.
 */
function LoopConditionRow({ condition, onChange, onRemove, canRemove }) {
    const fieldValue = condition.field || '';
    const conditionType = condition.type || getFieldType(fieldValue) || 'string';
    const operators = getOperatorsForType(conditionType);

    const handleFieldChange = (e) => {
        const newField = e.target.value;
        const known = FIELDS.find((f) => f.value === newField);
        if (known) {
            // Reset value when type changes (e.g. string -> number) so we
            // don't carry a stale string into a numeric expression.
            const typeChanged = conditionType !== known.type;
            onChange({
                ...condition,
                field: newField,
                type: known.type,
                operator: typeChanged || !condition.operator ? DEFAULT_OPERATOR : condition.operator,
                value: typeChanged ? getDefaultValue(known.type) : condition.value,
            });
            return;
        }
        // Empty / unknown field — keep current type so the row doesn't flicker.
        onChange({ ...condition, field: newField });
    };

    const handleOperatorChange = (e) =>
        onChange({ ...condition, operator: e.target.value });

    // The judge's `score` field is normalised 0.0–1.0 in the backend
    // (see backend/app/engine/loop_evaluator.py: judge scores are clamped
    // to `max(0.0, min(1.0, score_val))`). Without an input-side guard,
    // users naturally type "70" thinking percent — the condition compiles
    // to `input.score == 70`, which can NEVER be true, so the loop
    // silently misbehaves. This guard floors at 0, caps at 1, and pairs
    // with native min/max attrs on the <input> below.
    const isConfidenceScore = condition.field === 'score';
    const clampScore = (n) => Math.max(0, Math.min(1, n));

    const handleValueChange = (e) => {
        if (conditionType === 'boolean') {
            onChange({ ...condition, value: e.target.value === 'true' });
            return;
        }
        if (conditionType === 'number') {
            const v = e.target.value;
            if (v === '') {
                onChange({ ...condition, value: '' });
                return;
            }
            let n = parseFloat(v) || 0;
            if (isConfidenceScore) n = clampScore(n);
            onChange({ ...condition, value: n });
            return;
        }
        onChange({ ...condition, value: e.target.value });
    };

    const valueDisabled = !condition.field || !condition.operator;

    return (
        <div className="loop-row">
            <select
                className="form-select loop-row-field"
                value={fieldValue}
                onChange={handleFieldChange}
                title="Field"
                aria-label="Field"
            >
                <option value="">Select field</option>
                {FIELDS.map((f) => (
                    <option key={f.value} value={f.value}>{f.label}</option>
                ))}
            </select>

            <select
                className="form-select loop-row-op"
                value={condition.operator || ''}
                onChange={handleOperatorChange}
                disabled={!condition.field}
                title="Operator"
                aria-label="Operator"
            >
                <option value="">Op</option>
                {operators.map((op) => (
                    <option key={op.value} value={op.value}>{op.label}</option>
                ))}
            </select>

            {conditionType === 'boolean' ? (
                <select
                    className="form-select loop-row-value"
                    value={condition.value ? 'true' : 'false'}
                    onChange={handleValueChange}
                    disabled={valueDisabled}
                    aria-label="Value"
                >
                    <option value="true">True</option>
                    <option value="false">False</option>
                </select>
            ) : (
                <input
                    type={conditionType === 'number' ? 'number' : 'text'}
                    className="form-input loop-row-value"
                    value={condition.value ?? ''}
                    onChange={handleValueChange}
                    /* Confidence Score is a normalised 0.0–1.0 ratio
                       coming from the judge. We narrow the placeholder,
                       step, min, and max only for this one field so
                       generic numeric fields (amount, count) keep their
                       existing free-form behaviour. The handleValueChange
                       clamp above is the authoritative guard — these
                       native attrs just nudge the user to type the right
                       shape (browser shows up/down arrows in the 0–1
                       range, mobile shows the right keypad). */
                    placeholder={isConfidenceScore ? '0.0 – 1.0' : 'Value'}
                    disabled={valueDisabled}
                    step={
                        isConfidenceScore
                            ? '0.01'
                            : conditionType === 'number'
                                ? 'any'
                                : undefined
                    }
                    min={isConfidenceScore ? 0 : undefined}
                    max={isConfidenceScore ? 1 : undefined}
                    spellCheck={false}
                    aria-label="Value"
                    title={
                        isConfidenceScore
                            ? 'Confidence Score is a 0.0–1.0 ratio (e.g. 0.85 = 85%).'
                            : undefined
                    }
                />
            )}
            {isConfidenceScore && !valueDisabled && (
                /* Tiny inline hint under the row so the next user
                   understands the scale at a glance. Spans all 4 grid
                   columns so it sits cleanly below the row. */
                <div className="loop-row-hint">
                    Confidence Score is between <strong>0.0 and 1.0</strong>
                    {' '}(e.g. <code>0.85</code> means 85%).
                </div>
            )}

            {canRemove && (
                <button
                    className="loop-row-remove"
                    onClick={onRemove}
                    title="Remove condition"
                    type="button"
                    aria-label="Remove condition"
                >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                        <line x1="18" y1="6" x2="6" y2="18" />
                        <line x1="6" y1="6" x2="18" y2="18" />
                    </svg>
                </button>
            )}
        </div>
    );
}

export default LoopConditionRow;
