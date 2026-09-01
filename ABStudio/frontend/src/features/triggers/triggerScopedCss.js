// SPDX-License-Identifier: Apache-2.0
/**
 * High-specificity scoped CSS for the post-deploy / post-apply TriggerSection
 * rendered inside the Workflow Factory and Agent Factory modals. Injected as
 * an inline <style> block so it ships with the component and cannot be
 * silenced by load-order or specificity issues from the global stylesheets.
 *
 * Scope: any element under a `.factory-trigger-root` ancestor.
 */
const TRIGGER_SCOPED_CSS = `
.factory-trigger-root .trigger-section {
    margin-top: 0 !important;
    padding-top: 0 !important;
    border-top: none !important;
    background: transparent !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 14px !important;
}
.factory-trigger-root .trigger-section h2.agent-config-section-title,
.factory-trigger-root .trigger-section .agent-config-section-title {
    display: block !important;
    font-size: 13px !important;
    font-weight: 700 !important;
    color: #0f172a !important;
    margin: 0 0 4px !important;
    padding: 0 !important;
    border: none !important;
    letter-spacing: 0 !important;
    text-transform: none !important;
}
.factory-trigger-root .trigger-section .trigger-section-hint {
    display: block !important;
    margin-top: 4px !important;
    font-size: 11.5px !important;
    font-weight: 400 !important;
    color: #64748b !important;
    letter-spacing: 0 !important;
    text-transform: none !important;
}
.factory-trigger-root .trigger-section .trigger-section-empty {
    display: block !important;
    margin: 0 !important;
    padding: 10px 12px !important;
    background: #f8fafc !important;
    border: 1px dashed #e2e8f0 !important;
    border-radius: 8px !important;
    font-size: 12px !important;
    color: #94a3b8 !important;
}
.factory-trigger-root .trigger-section .trigger-add-btn {
    display: block !important;
    width: 100% !important;
    margin-top: 4px !important;
    padding: 10px 14px !important;
    background: rgba(99, 102, 241, 0.04) !important;
    border: 1px dashed rgba(99, 102, 241, 0.45) !important;
    border-radius: 10px !important;
    color: #4f46e5 !important;
    font-size: 13px !important;
    font-weight: 600 !important;
    cursor: pointer !important;
    transition: background 0.18s ease, border-color 0.18s ease !important;
    text-align: center !important;
    font-family: inherit !important;
}
.factory-trigger-root .trigger-section .trigger-add-btn:hover {
    background: rgba(99, 102, 241, 0.10) !important;
    border-color: rgba(99, 102, 241, 0.75) !important;
}
.factory-trigger-root .trigger-section .trigger-card {
    background: #ffffff !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 12px !important;
    padding: 12px 14px !important;
    margin: 0 !important;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04) !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 10px !important;
}
.factory-trigger-root .trigger-section .trigger-card.is-draft {
    border: 1px dashed rgba(99, 102, 241, 0.5) !important;
    background: linear-gradient(180deg, rgba(99, 102, 241, 0.04), #fff) !important;
}
.factory-trigger-root .trigger-section .trigger-card-header {
    display: flex !important;
    align-items: center !important;
    gap: 8px !important;
    margin: 0 !important;
}
.factory-trigger-root .trigger-section .trigger-card-name {
    flex: 1 1 auto !important;
    min-width: 0 !important;
    padding: 7px 10px !important;
    background: #f8fafc !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 8px !important;
    color: #0f172a !important;
    font-size: 12.5px !important;
    font-weight: 500 !important;
    font-family: inherit !important;
}
.factory-trigger-root .trigger-section .trigger-card-input {
    width: 100% !important;
    padding: 8px 10px !important;
    background: #fff !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 8px !important;
    color: #0f172a !important;
    font-size: 13px !important;
    font-family: ui-monospace, monospace !important;
    box-sizing: border-box !important;
}
.factory-trigger-root .trigger-section .trigger-card-input-label {
    display: flex !important;
    flex-direction: column !important;
    gap: 4px !important;
    font-size: 11px !important;
    font-weight: 600 !important;
    color: #64748b !important;
    text-transform: uppercase !important;
    letter-spacing: 0.04em !important;
}
.factory-trigger-root .trigger-section .trigger-draft-actions {
    display: flex !important;
    justify-content: flex-end !important;
    gap: 8px !important;
    margin-top: 4px !important;
}
.factory-trigger-root .trigger-section .trigger-draft-cancel {
    padding: 7px 14px !important;
    background: transparent !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 8px !important;
    color: #475569 !important;
    font-size: 12.5px !important;
    font-weight: 600 !important;
    cursor: pointer !important;
    font-family: inherit !important;
}
.factory-trigger-root .trigger-section .trigger-draft-save {
    padding: 7px 16px !important;
    background: linear-gradient(135deg, #4f46e5, #7c3aed) !important;
    border: none !important;
    border-radius: 8px !important;
    color: #fff !important;
    font-size: 12.5px !important;
    font-weight: 600 !important;
    cursor: pointer !important;
    font-family: inherit !important;
}
.factory-trigger-root .trigger-section .trigger-card-toggle {
    display: inline-flex !important;
    align-items: center !important;
    gap: 6px !important;
    padding: 4px 10px !important;
    background: #f1f5f9 !important;
    border-radius: 999px !important;
    font-size: 11px !important;
    font-weight: 600 !important;
    color: #475569 !important;
    cursor: pointer !important;
}
.factory-trigger-root .trigger-section .trigger-card-history-btn,
.factory-trigger-root .trigger-section .trigger-card-delete {
    display: inline-flex !important;
    align-items: center !important;
    gap: 4px !important;
    padding: 5px 10px !important;
    background: #ffffff !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 8px !important;
    color: #475569 !important;
    font-size: 11.5px !important;
    font-weight: 600 !important;
    cursor: pointer !important;
    font-family: inherit !important;
}
.factory-trigger-root .trigger-section .trigger-card-history-btn:hover,
.factory-trigger-root .trigger-section .trigger-card-delete:hover {
    border-color: #4f46e5 !important;
    color: #4f46e5 !important;
}
.factory-trigger-root .trigger-section .trigger-card-error {
    padding: 6px 10px !important;
    font-size: 11.5px !important;
    color: #b91c1c !important;
    background: #fef2f2 !important;
    border: 1px solid #fecaca !important;
    border-radius: 6px !important;
}
`;

export default TRIGGER_SCOPED_CSS;
