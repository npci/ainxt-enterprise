// SPDX-License-Identifier: MIT
/**
 * Factory functions for condition-node domain objects.
 *
 * Shape must stay aligned with the backend evaluator —
 * see ABStudio/backend/app/services/services.py::build_expression_from_case.
 */
import { makeId } from '../../../../utils/makeId';

export function newConditionRow(overrides = {}) {
    return {
        id: makeId('cond'),
        field: '',
        operator: '',
        value: '',
        type: 'string',
        ...overrides,
    };
}

/**
 * Row used by Simple mode: `intent contains "<topic>"`.
 */
export function newSimpleConditionRow(topic = '') {
    return newConditionRow({
        field: 'intent',
        operator: 'contains',
        value: topic,
        type: 'string',
    });
}

export function newCase(label) {
    return {
        id: makeId('case'),
        label,
        conditions: [newSimpleConditionRow()],
        logic: 'AND',
    };
}

/**
 * Returns true when a case can be edited via the simple "If the message is
 * about X" input. A case is simple iff it has exactly one condition row
 * shaped as `intent contains <something>`.
 */
export function isSimpleCase(caseData) {
    const rows = caseData?.conditions || [];
    if (rows.length !== 1) return false;
    const r = rows[0];
    return r.field === 'intent' && r.operator === 'contains' && (r.type === 'string' || !r.type);
}
