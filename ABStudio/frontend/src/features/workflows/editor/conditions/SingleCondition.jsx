// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react';
import {
    FIELDS,
    OPERATORS,
    getFieldType,
    getOperatorsForType,
    getDefaultValue,
    DEFAULT_OPERATOR,
} from '../../../../constants/operators';

// Sentinel used by the Field <select> to switch into free-text ("custom") mode.
const CUSTOM_FIELD = '__custom__';

// Numeric-only operators. Picking one of these infers `type = number` so the
// backend evaluates a numeric comparison — this is how we drop the separate
// "Type" dropdown while keeping the underlying data shape intact.
const NUMERIC_OPERATORS = new Set(['>', '>=', '<', '<=']);

// The operator dropdown shows every option that makes sense across text and
// number fields. Boolean-only operators are a subset of the string ops (==,!=)
// so they need no special entry here.
const ALL_OPERATORS = [
    ...OPERATORS.string,
    ...OPERATORS.number.filter(
        (op) => !OPERATORS.string.some((s) => s.value === op.value),
    ),
];

/**
 * SingleCondition — one row of the Advanced builder, redesigned for clarity.
 *
 * Layout (clearly labeled, no cryptic controls):
 *   Field      → combobox: preset fields + "Custom field…" (free text)
 *   Condition  → operator dropdown (plain-English labels)
 *   Value      → text / number / true-false depending on inferred type
 *
 * The value `type` is inferred and stored silently:
 *   • known preset field → its declared type
 *   • numeric operator chosen → number
 *   • otherwise → string
 * so downstream code (build_expression_from_case) is unchanged.
 */
function SingleCondition({ condition, onChange, onRemove, canRemove }) {
    const conditionType = condition.type || getFieldType(condition.field) || 'string';

    // A field is "custom" when it isn't one of the presets. Track an explicit
    // flag so an empty custom field (mid-typing) doesn't snap back to the
    // dropdown on every keystroke.
    const isKnownField = FIELDS.some((f) => f.value === condition.field);
    const [customMode, setCustomMode] = useState(
        () => !isKnownField && !!condition.field,
    );
    const showCustomInput = customMode || (!isKnownField && !!condition.field);

    // For a known preset field, offer only the operators that fit its type
    // (e.g. Confidence Score → numeric comparisons, no "contains"). For custom
    // free-text fields we can't know the type up front, so offer the full set
    // and infer the type from whichever operator the user picks.
    const operators = isKnownField
        ? getOperatorsForType(conditionType)
        : ALL_OPERATORS;

    const handlePresetChange = (e) => {
        const picked = e.target.value;
        if (picked === CUSTOM_FIELD) {
            setCustomMode(true);
            onChange({ ...condition, field: '', type: 'string' });
            return;
        }
        setCustomMode(false);
        const known = FIELDS.find((f) => f.value === picked);
        const nextType = known ? known.type : 'string';
        const opStillValid = getOperatorsForType(nextType).some(
            (op) => op.value === condition.operator,
        );
        onChange({
            ...condition,
            field: picked,
            type: nextType,
            operator: opStillValid ? condition.operator : DEFAULT_OPERATOR,
            value: condition.value === '' || condition.value == null
                ? getDefaultValue(nextType)
                : condition.value,
        });
    };

    const handleCustomFieldChange = (e) => {
        onChange({ ...condition, field: e.target.value });
    };

    const handleOperatorChange = (e) => {
        const op = e.target.value;
        // Infer type from the operator so we don't need a Type dropdown.
        let nextType = conditionType;
        if (NUMERIC_OPERATORS.has(op)) {
            nextType = 'number';
        } else if (conditionType === 'number' && !isKnownField) {
            // Leaving a numeric op on a custom field → back to text.
            nextType = 'string';
        }
        let nextValue = condition.value;
        if (nextType === 'number' && typeof nextValue !== 'number') {
            nextValue = nextValue === '' || nextValue == null
                ? ''
                : (parseFloat(nextValue) || 0);
        }
        onChange({ ...condition, operator: op, type: nextType, value: nextValue });
    };

    const handleValueChange = (e) => {
        if (conditionType === 'boolean') {
            onChange({ ...condition, value: e.target.value === 'true' });
            return;
        }
        onChange({ ...condition, value: e.target.value });
    };

    const selectValue = showCustomInput
        ? CUSTOM_FIELD
        : (isKnownField ? condition.field : '');
    const valueDisabled = !condition.field || !condition.operator;

    return (
        <div className="cb-row">
            {/* ── Field ── */}
            <div className="cb-field">
                <label className="cb-field-label">Field to check</label>
                <div className="cb-field-controls">
                    <select
                        className="form-select cb-field-select"
                        value={selectValue}
                        onChange={handlePresetChange}
                        title="Which value from the previous step to check"
                    >
                        <option value="" disabled>Choose a field…</option>
                        {FIELDS.map((f) => (
                            <option key={f.value} value={f.value}>{f.label}</option>
                        ))}
                        <option value={CUSTOM_FIELD}>Custom field…</option>
                    </select>
                    {canRemove ? (
                        <button
                            className="cb-row-remove"
                            onClick={onRemove}
                            title="Remove this condition"
                            type="button"
                        >
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                                <line x1="18" y1="6" x2="6" y2="18" />
                                <line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                        </button>
                    ) : (
                        <span className="cb-row-remove cb-row-remove--placeholder" aria-hidden="true" />
                    )}
                </div>
                {showCustomInput && (
                    <input
                        type="text"
                        className="form-input cb-field-custom"
                        value={condition.field || ''}
                        onChange={handleCustomFieldChange}
                        placeholder="Type a field name (e.g. order_status)"
                        spellCheck={false}
                        autoComplete="off"
                        autoFocus
                    />
                )}
            </div>

            {/* ── Condition + Value ── */}
            <div className="cb-row-line--bottom">
                <div className="cb-field cb-field--op">
                    <label className="cb-field-label">Condition</label>
                    <select
                        className="form-select"
                        value={condition.operator || ''}
                        onChange={handleOperatorChange}
                        disabled={!condition.field}
                    >
                        <option value="" disabled>Choose…</option>
                        {operators.map((op) => (
                            <option key={op.value} value={op.value}>{op.label}</option>
                        ))}
                    </select>
                </div>

                <div className="cb-field cb-field--val">
                    <label className="cb-field-label">Value</label>
                    {conditionType === 'boolean' ? (
                        <select
                            className="form-select"
                            value={condition.value ? 'true' : 'false'}
                            onChange={handleValueChange}
                            disabled={valueDisabled}
                        >
                            <option value="true">True</option>
                            <option value="false">False</option>
                        </select>
                    ) : (
                        <input
                            type={conditionType === 'number' ? 'number' : 'text'}
                            className="form-input"
                            value={condition.value ?? ''}
                            onChange={handleValueChange}
                            placeholder={conditionType === 'number' ? '0' : 'e.g. billing'}
                            disabled={valueDisabled}
                            step={conditionType === 'number' ? 'any' : undefined}
                            spellCheck={false}
                        />
                    )}
                </div>
            </div>
        </div>
    );
}

export default SingleCondition;
