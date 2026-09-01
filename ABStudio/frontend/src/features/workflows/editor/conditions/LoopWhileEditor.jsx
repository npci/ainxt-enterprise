// SPDX-License-Identifier: Apache-2.0
import LoopConditionRow from './LoopConditionRow';
import {
    newConditionRow,
    newCase,
} from './factories';
import { buildCombinedExpressionPreview, DEFAULT_OPERATOR } from '../../../../constants/operators';
import '../../../../styles/ConditionBuilder.css';
import '../../../../styles/LoopWhileEditor.css';

/**
 * LoopWhileEditor — single-card condition editor for a Loop node's
 * "continue while" expression.
 *
 * This is intentionally NOT a `ConditionBuilder`. A loop has exactly one
 * continuation predicate, so the routing-rule machinery (multi-case top-down,
 * ELSE fallback, per-case Simple/Advanced toggle, "Add Case" button) is all
 * irrelevant here. Rendering it leaks routing semantics into the loop config
 * and confuses users.
 *
 * Layout matches the original Loop screenshot:
 *   - "Continue while" section header
 *   - One condition card containing one-line rows (field-select + op + value)
 *   - "+ Add condition" button
 *   - "EVALUATES TO" preview
 *
 * Data is still persisted to ``data.cases`` as a single-element array so the
 * backend's `_run_loop` keeps working with no schema change —
 * `build_expression_from_case` runs over that one case identically to a
 * Conditional case.
 */
function LoopWhileEditor({ cases, onChange }) {
    // The Loop store initialises ``cases`` as ``[]``. Seed a default case
    // with one empty row using the `==` operator (matches the screenshot's
    // pre-populated `equals` default) so the editor opens with something
    // sensible to fill in.
    const activeCase = cases && cases.length > 0
        ? cases[0]
        : {
            ...newCase('Loop condition'),
            conditions: [newConditionRow({ operator: DEFAULT_OPERATOR })],
        };

    const conditions = activeCase.conditions || [];
    const logic = activeCase.logic || 'AND';
    const expressionPreview = buildCombinedExpressionPreview(conditions, logic);

    const emit = (nextCase) => {
        onChange([nextCase]);
    };

    const handleLogicChange = (newLogic) =>
        emit({ ...activeCase, logic: newLogic });

    const handleConditionChange = (i, updated) => {
        const next = [...conditions];
        next[i] = updated;
        emit({ ...activeCase, conditions: next });
    };

    const handleAddCondition = () =>
        emit({
            ...activeCase,
            conditions: [
                ...conditions,
                newConditionRow({ operator: DEFAULT_OPERATOR }),
            ],
        });

    const handleRemoveCondition = (i) =>
        emit({
            ...activeCase,
            conditions: conditions.filter((_, idx) => idx !== i),
        });

    return (
        <div className="loop-while-editor">
            <div className="lwe-card">
                <div className="lwe-header">
                    <span className="lwe-header-title">Continue while</span>
                    {conditions.length >= 2 && (
                        <div className="cb-logic" title="How to combine the conditions">
                            <button
                                type="button"
                                className={`cb-logic-pill${logic === 'AND' ? ' active' : ''}`}
                                onClick={() => handleLogicChange('AND')}
                            >
                                ALL (AND)
                            </button>
                            <button
                                type="button"
                                className={`cb-logic-pill${logic === 'OR' ? ' active' : ''}`}
                                onClick={() => handleLogicChange('OR')}
                            >
                                ANY (OR)
                            </button>
                        </div>
                    )}
                </div>

                <div className="lwe-body">
                    {conditions.length === 0 ? (
                        <div className="lwe-empty">
                            No condition yet — add one below.
                        </div>
                    ) : (
                        conditions.map((cond, i) => (
                            <div key={cond.id || i} className="lwe-row-group">
                                {i > 0 && (
                                    <div className="lwe-row-connector" aria-hidden="true">
                                        {logic === 'OR' ? 'or' : 'and'}
                                    </div>
                                )}
                                <LoopConditionRow
                                    condition={cond}
                                    onChange={(u) => handleConditionChange(i, u)}
                                    onRemove={() => handleRemoveCondition(i)}
                                    canRemove={conditions.length > 1}
                                />
                            </div>
                        ))
                    )}
                </div>
            </div>

            <button className="lwe-add-row" onClick={handleAddCondition} type="button">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                    <line x1="12" y1="5" x2="12" y2="19" />
                    <line x1="5" y1="12" x2="19" y2="12" />
                </svg>
                Add condition
            </button>

            <div className="lwe-expr">
                <div className="lwe-expr-label">EVALUATES TO</div>
                <code className="lwe-expr-code">{expressionPreview || "''"}</code>
            </div>
        </div>
    );
}

export default LoopWhileEditor;
