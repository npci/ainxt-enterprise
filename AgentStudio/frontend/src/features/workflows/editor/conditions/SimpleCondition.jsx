// SPDX-License-Identifier: MIT
/**
 * SimpleCondition — non-engineer friendly editor for one case.
 *
 * The user types a topic in plain English (e.g. "billing", "technical support").
 * Internally we serialise this as the same structured condition shape the
 * backend already understands:
 *   { field: 'intent', operator: 'contains', value: <topic>, type: 'string' }
 */
function SimpleCondition({ value, onChange }) {
    return (
        <div className="cb-simple">
            <label className="cb-simple-label">
                If the message is about
            </label>
            <input
                type="text"
                className="form-input cb-simple-input"
                value={value || ''}
                onChange={(e) => onChange(e.target.value)}
                placeholder="e.g. billing, refunds, technical support"
                spellCheck={false}
                autoComplete="off"
            />
            <p className="cb-simple-hint">
                The previous step should classify the message. Type a word or
                short phrase that describes this branch.
            </p>
        </div>
    );
}

export default SimpleCondition;
