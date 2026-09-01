// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react';
import SingleCondition from './SingleCondition';
import SimpleCondition from './SimpleCondition';
import {
    newConditionRow,
    newSimpleConditionRow,
    isSimpleCase,
} from './factories';
import { buildPlainEnglishPreview } from '../../../../constants/operators';

/**
 * ConditionCase — one IF-branch card.  Mirrors the config-collapse aesthetic
 * used by the agent config's "Model Parameters" / "Catalog Tools" sections.
 */
function ConditionCase({ caseData, caseNumber, onChange, onRemove, canRemove }) {
    const conditions = caseData.conditions || [];
    const logic = caseData.logic || 'AND';
    const labelValue = caseData.label ?? caseData.name ?? '';
    const expressionPreview = buildPlainEnglishPreview(conditions, logic);
    const [simpleMode, setSimpleMode] = useState(() => isSimpleCase(caseData));

    const handleLabelChange = (e) =>
        onChange({ ...caseData, label: e.target.value });

    const handleLogicChange = (newLogic) =>
        onChange({ ...caseData, logic: newLogic });

    const handleConditionChange = (i, updated) => {
        const next = [...conditions];
        next[i] = updated;
        onChange({ ...caseData, conditions: next });
    };

    const handleAddCondition = () =>
        onChange({
            ...caseData,
            conditions: [...conditions, newConditionRow()],
        });

    const handleRemoveCondition = (i) =>
        onChange({ ...caseData, conditions: conditions.filter((_, idx) => idx !== i) });

    const simpleValue = simpleMode ? (conditions[0]?.value || '') : '';

    const handleSimpleChange = (topic) => {
        const existing = conditions[0];
        const nextRow = existing && existing.id
            ? { ...existing, field: 'intent', operator: 'contains', value: topic, type: 'string' }
            : newSimpleConditionRow(topic);
        onChange({
            ...caseData,
            conditions: [nextRow],
            logic: 'AND',
        });
    };

    const handleModeChange = (nextSimple) => {
        if (nextSimple === simpleMode) return;
        if (nextSimple) {
            const first = conditions[0];
            const seed = typeof first?.value === 'string' ? first.value : '';
            const nextRow = first && first.id
                ? { ...first, field: 'intent', operator: 'contains', value: seed, type: 'string' }
                : newSimpleConditionRow(seed);
            onChange({
                ...caseData,
                conditions: [nextRow],
                logic: 'AND',
            });
        }
        setSimpleMode(nextSimple);
    };

    return (
        <div className="cb-card">
            <div className="cb-card-head">
                <div className="cb-card-head-left">
                    <span className="cb-badge">{caseNumber}</span>
                    <input
                        type="text"
                        className="form-input cb-name-input"
                        value={labelValue}
                        onChange={handleLabelChange}
                        placeholder={`Case ${caseNumber}`}
                    />
                </div>
                {canRemove && (
                    <button className="cb-card-remove" onClick={onRemove} title="Remove case" type="button">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                            <line x1="18" y1="6" x2="6" y2="18" />
                            <line x1="6" y1="6" x2="18" y2="18" />
                        </svg>
                    </button>
                )}
            </div>

            <div className="cb-mode-row">
                <div className="cb-logic" role="tablist" aria-label="Editor mode">
                    <button
                        type="button"
                        className={`cb-logic-pill${simpleMode ? ' active' : ''}`}
                        onClick={() => handleModeChange(true)}
                    >
                        Simple
                    </button>
                    <button
                        type="button"
                        className={`cb-logic-pill${!simpleMode ? ' active' : ''}`}
                        onClick={() => handleModeChange(false)}
                    >
                        Advanced
                    </button>
                </div>
            </div>

            <div className="cb-card-body">
                {simpleMode ? (
                    <SimpleCondition value={simpleValue} onChange={handleSimpleChange} />
                ) : (
                    <>
                        <div className="cb-when-row">
                            <label className="form-label">Take this branch when…</label>
                            {conditions.length >= 2 && (
                                <div className="cb-logic" title="How to combine the conditions in this case">
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

                        <div className="cb-rows">
                            {conditions.length === 0 ? (
                                <div className="cb-rows-empty">
                                    No condition yet — add one below.
                                </div>
                            ) : (
                                conditions.map((cond, i) => (
                                    <div key={cond.id || i} className="cb-row-group">
                                        {i > 0 && (
                                            <div className="cb-row-connector" aria-hidden="true">
                                                {logic === 'OR' ? 'or' : 'and'}
                                            </div>
                                        )}
                                        <SingleCondition
                                            condition={cond}
                                            onChange={(u) => handleConditionChange(i, u)}
                                            onRemove={() => handleRemoveCondition(i)}
                                            canRemove={conditions.length > 1}
                                        />
                                    </div>
                                ))
                            )}
                        </div>

                        <button className="cb-add-row" onClick={handleAddCondition} type="button">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                                <line x1="12" y1="5" x2="12" y2="19" />
                                <line x1="5" y1="12" x2="19" y2="12" />
                            </svg>
                            Add condition
                        </button>
                    </>
                )}
            </div>

            {!simpleMode && expressionPreview && (
                <div className="cb-expr">
                    <span className="cb-expr-label">In plain English</span>
                    <span className="cb-expr-text">{expressionPreview}</span>
                </div>
            )}
        </div>
    );
}

export default ConditionCase;
