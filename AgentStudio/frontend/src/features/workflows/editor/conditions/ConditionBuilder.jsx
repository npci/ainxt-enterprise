// SPDX-License-Identifier: MIT
import ConditionCase from './ConditionCase';
import { newCase } from './factories';
import '../../../../styles/ConditionBuilder.css';

/**
 * ConditionBuilder — visual condition rule editor for the ConfigPanel.
 * Mirrors the structure of the agent config: form-group → form-label → content.
 */
function ConditionBuilder({ cases, onChange }) {
    const handleCaseChange = (caseIndex, updatedCase) => {
        const newCases = [...cases];
        newCases[caseIndex] = updatedCase;
        onChange(newCases);
    };

    const handleAddCase = () => {
        onChange([...cases, newCase(`Case ${cases.length + 1}`)]);
    };

    const handleRemoveCase = (caseIndex) => {
        const newCases = cases.filter((_, i) => i !== caseIndex);
        onChange(newCases);
    };

    return (
        <div className="cb-root">
            {/* Intro */}
            <div className="form-group">
                <label className="form-label">Routing Rules</label>
                <span className="form-hint">
                    Each case is evaluated top-to-bottom. The first match wins. Unmatched
                    inputs fall through to the <strong>Else</strong> branch.
                </span>
            </div>

            {/* Cases */}
            <div className="cb-cases">
                {cases.map((caseData, index) => (
                    <ConditionCase
                        key={caseData.id || index}
                        caseData={caseData}
                        caseNumber={index + 1}
                        onChange={(updated) => handleCaseChange(index, updated)}
                        onRemove={() => handleRemoveCase(index)}
                        canRemove={cases.length > 1}
                    />
                ))}
            </div>

            {/* Add case */}
            <button className="cb-add-case" onClick={handleAddCase} type="button">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                    <line x1="12" y1="5" x2="12" y2="19" />
                    <line x1="5" y1="12" x2="19" y2="12" />
                </svg>
                Add Case
            </button>

            {/* Else fallback */}
            <div className="cb-else">
                <span className="cb-else-tag">ELSE</span>
                <span className="cb-else-text">Default path — taken when no conditions match</span>
            </div>
        </div>
    );
}

export default ConditionBuilder;
