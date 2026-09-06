// SPDX-License-Identifier: MIT
/**
 * Field, type, and operator definitions for the condition builder.
 * FIELDS is a suggestion list (rendered as a datalist) — the user can
 * type any field. Each condition carries its own explicit `type`.
 */

export const FIELDS = [
    { value: 'intent', label: 'Intent', type: 'string' },
    { value: 'category', label: 'Category', type: 'string' },
    { value: 'sentiment', label: 'Sentiment', type: 'string' },
    { value: 'language', label: 'Language', type: 'string' },
    { value: 'priority', label: 'Priority', type: 'string' },
    { value: 'customer_tier', label: 'Customer Tier', type: 'string' },
    { value: 'urgency', label: 'Urgency', type: 'string' },
    { value: 'score', label: 'Confidence Score', type: 'number' },
    { value: 'amount', label: 'Amount', type: 'number' },
    { value: 'count', label: 'Count', type: 'number' },
];

export const OPERATORS = {
    string: [
        { value: '==', label: 'equals' },
        { value: '!=', label: 'not equals' },
        { value: 'contains', label: 'contains' },
        { value: 'not_contains', label: 'does not contain' },
    ],
    number: [
        { value: '==', label: 'equals' },
        { value: '!=', label: 'not equals' },
        { value: '>', label: 'greater than' },
        { value: '>=', label: 'greater or equal' },
        { value: '<', label: 'less than' },
        { value: '<=', label: 'less or equal' },
    ],
    boolean: [
        { value: '==', label: 'is' },
        { value: '!=', label: 'is not' },
    ],
};

export const DEFAULT_OPERATOR = '==';

export function getFieldType(fieldValue) {
    const field = FIELDS.find((f) => f.value === fieldValue);
    return field ? field.type : 'string';
}

export function getOperatorsForType(type) {
    return OPERATORS[type] || OPERATORS.string;
}

export function getDefaultValue(type) {
    if (type === 'number') return 0;
    if (type === 'boolean') return true;
    return '';
}

// Coerce a raw form value to the canonical JS shape for its declared type.
// Kept next to getDefaultValue so the string→number/boolean rules used at
// form-fill time live alongside the new-row defaults — both must stay in
// sync with backend services._build_single_expression.
export function coerceValueByType(raw, type) {
    if (type === 'number') return parseFloat(raw) || 0;
    if (type === 'boolean') return raw === true || raw === 'true';
    return raw;
}

/**
 * Build expression preview for one condition.
 * MUST stay in sync with backend services._build_single_expression so the
 * preview matches what the engine actually evaluates.
 */
export function buildExpressionPreview(condition) {
    const { field, operator, value, type } = condition;
    if (!field || !operator) return '';

    let formattedValue;
    if (type === 'number') {
        formattedValue = value === '' || value == null ? '0' : String(value);
    } else if (type === 'boolean') {
        formattedValue = value ? 'True' : 'False';
    } else {
        formattedValue = `'${String(value ?? '').replace(/'/g, "\\'")}'`;
    }

    if (operator === 'contains') return `${formattedValue} in input.${field}`;
    if (operator === 'not_contains') return `${formattedValue} not in input.${field}`;
    return `input.${field} ${operator} ${formattedValue}`;
}

export function buildCombinedExpressionPreview(conditions, logic) {
    if (!conditions || conditions.length === 0) return '';

    const expressions = conditions
        .filter((c) => c.field && c.operator)
        .map((c) => buildExpressionPreview(c))
        .filter(Boolean);

    if (expressions.length === 0) return '';
    if (expressions.length === 1) return expressions[0];
    return expressions.join(logic === 'OR' ? ' or ' : ' and ');
}

// ---------------------------------------------------------------------------
// Plain-English preview — human-readable summary shown in the config panel so
// non-engineers understand a rule without reading the raw expression syntax.
// ---------------------------------------------------------------------------

// Operator → natural-language verb. Keep the keys aligned with OPERATORS.
const OPERATOR_VERBS = {
    '==': 'is',
    '!=': 'is not',
    contains: 'contains',
    not_contains: 'does not contain',
    '>': 'is greater than',
    '>=': 'is at least',
    '<': 'is less than',
    '<=': 'is at most',
};

// Pretty label for a field: use the preset label when the field is known,
// otherwise Title-Case the raw field name (e.g. "order_status" → "Order status").
function fieldLabel(field) {
    if (!field) return 'the field';
    const known = FIELDS.find((f) => f.value === field);
    if (known) return known.label;
    const spaced = String(field).replace(/[_.]+/g, ' ').trim();
    return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : field;
}

/**
 * One condition → plain English, e.g. `Intent contains "billing"`.
 * Returns '' when the field is missing so callers can skip incomplete rows.
 */
export function buildPlainEnglishCondition(condition) {
    const { field, operator, value, type } = condition || {};
    if (!field || !operator) return '';

    const verb = OPERATOR_VERBS[operator] || operator;
    const label = fieldLabel(field);

    let valuePart;
    if (type === 'boolean') {
        valuePart = value ? 'true' : 'false';
    } else if (type === 'number') {
        valuePart = value === '' || value == null ? '…' : String(value);
    } else {
        const v = String(value ?? '').trim();
        valuePart = v ? `"${v}"` : '…';
    }
    return `${label} ${verb} ${valuePart}`;
}

/**
 * All rows in a case → a single "When …" sentence.
 * e.g. `When Intent contains "billing" and Priority is "high"`.
 */
export function buildPlainEnglishPreview(conditions, logic) {
    if (!conditions || conditions.length === 0) return '';
    const parts = conditions
        .filter((c) => c.field && c.operator)
        .map(buildPlainEnglishCondition)
        .filter(Boolean);
    if (parts.length === 0) return '';
    const joined = parts.join(logic === 'OR' ? ' or ' : ' and ');
    return `When ${joined}`;
}
