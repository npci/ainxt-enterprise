// SPDX-License-Identifier: MIT
// RunSettingsStrip — chat-header icon button + popover for workflow-wide
// "Run settings" applied to the NEXT execute from this chat. Today it hosts
// one knob:
//
//   * Use subagents (swarm) — enable LLM-driven sub-task delegation
//
// Designed to host more knobs (max iterations, temperature presets,
// model overrides) without re-laying the chrome — just add another
// `.run-settings-field` row inside the popover.
//
// Resolution order at execution time (enforced server-side):
//   1. Per-node OFF pin (`data.disable_subagents = true`) → that node forced OFF
//   2. Per-node ON  pin (`data.enable_subagents  = true`) → that node forced ON
//                                                           even when run-level
//                                                           flag is OFF
//   3. This run-level flag from the store                 → applies to all
//                                                           otherwise-unpinned nodes
//   4. Nothing set                                        → engine default
//
// Visual contract: the trigger sits as a third `chat-icon-btn` alongside
// the History (clock) and New-chat (+) buttons; clicking it opens an
// anchored popover with a section header, a structured field row, and a
// short context caption. The icon shows a small status dot when subagents
// are ON so users can read the run policy at a glance without opening.
import { useState, useRef, useEffect, useCallback } from 'react';
import useWorkflowStore from '../../../store/workflowStore';

function RunSettingsStrip() {
    const [open, setOpen] = useState(false);
    const enabled = useWorkflowStore((s) => s.runSubagentsEnabled);
    const setEnabled = useWorkflowStore((s) => s.setRunSubagentsEnabled);

    const popoverRef = useRef(null);
    const buttonRef  = useRef(null);

    // Outside-click + Escape: standard popover dismiss semantics. Capture
    // phase so we beat React's onClick bubbling on the trigger button.
    useEffect(() => {
        if (!open) return undefined;
        const onDocClick = (e) => {
            if (popoverRef.current && popoverRef.current.contains(e.target)) return;
            if (buttonRef.current  && buttonRef.current.contains(e.target))  return;
            setOpen(false);
        };
        const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
        document.addEventListener('mousedown', onDocClick, true);
        document.addEventListener('keydown', onKey);
        return () => {
            document.removeEventListener('mousedown', onDocClick, true);
            document.removeEventListener('keydown', onKey);
        };
    }, [open]);

    const toggleOpen = useCallback(() => setOpen((v) => !v), []);
    const onToggleSwitch = useCallback(
        (e) => setEnabled(e.target.checked),
        [setEnabled],
    );

    return (
        <div className="run-settings-anchor">
            {/* Trigger — third icon button beside History / New-chat. Same
                .chat-icon-btn class so size, hover, and focus ring match. */}
            <button
                ref={buttonRef}
                type="button"
                className={`chat-icon-btn run-settings-trigger${open ? ' active' : ''}`}
                onClick={toggleOpen}
                title="Run settings"
                aria-label="Run settings"
                aria-haspopup="dialog"
                aria-expanded={open}
            >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <circle cx="12" cy="12" r="3" />
                    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
                </svg>
                {/* Status dot: tiny indigo dot on the gear when subagents
                    are ON. Lets users read the run policy at a glance
                    without opening the popover. */}
                {enabled && (
                    <span className="run-settings-trigger-dot" aria-hidden="true" />
                )}
            </button>

            {open && (
                <div
                    ref={popoverRef}
                    className="run-settings-popover"
                    role="dialog"
                    aria-label="Run settings"
                >
                    {/* Brand header — soft indigo gradient band with an
                        icon badge on the left, kicker + title + subtitle
                        stacked on the right. Reads more like a settings
                        sheet than a tooltip; matches the visual weight of
                        the rest of the editor chrome. */}
                    <div className="run-settings-popover-header">
                        <div className="run-settings-popover-badge" aria-hidden="true">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <circle cx="12" cy="12" r="3" />
                                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
                            </svg>
                        </div>
                        <div className="run-settings-popover-header-text">
                            <div className="run-settings-popover-kicker">
                                Workflow
                            </div>
                            <div className="run-settings-popover-title">
                                Run settings
                            </div>
                            <div className="run-settings-popover-subtitle">
                                Apply to the next run from this chat
                            </div>
                        </div>
                    </div>

                    {/* Body — structured "card row" with a left accent rail
                        that lights up indigo when subagents are enabled.
                        The card holds title + chip + sub + hint + meta;
                        the switch is right-aligned in its own column.
                        Future knobs add another .run-settings-card row
                        with the same structure. */}
                    <div className="run-settings-popover-body">
                        <div className="run-settings-section-kicker">
                            <span className="run-settings-section-bar" aria-hidden="true" />
                            Execution policy
                        </div>
                        <div className={`run-settings-card ${enabled ? 'run-settings-card--on' : ''}`}>
                            <div className="run-settings-card-text">
                                <div className="run-settings-field-label">
                                    <span
                                        className="run-settings-field-icon"
                                        aria-hidden="true"
                                    >
                                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                            <circle cx="9"  cy="7"  r="3" />
                                            <circle cx="17" cy="7"  r="3" />
                                            <circle cx="9"  cy="17" r="3" />
                                            <circle cx="17" cy="17" r="3" />
                                            <path d="M12 10v4" />
                                        </svg>
                                    </span>
                                    <span className="run-settings-field-title">
                                        Subagent delegation
                                    </span>
                                    <span
                                        className={`run-settings-chip ${enabled ? 'run-settings-chip--on' : 'run-settings-chip--off'}`}
                                    >
                                        <span
                                            className="run-settings-chip-dot"
                                            aria-hidden="true"
                                        />
                                        {enabled ? 'Enabled' : 'Disabled'}
                                    </span>
                                </div>
                                <div className="run-settings-field-sub">
                                    Use subagents (swarm) for this workflow run
                                </div>
                                {/* Hint copy MUST stay in sync with the
                                    per-node toggle in ConfigPanel.jsx
                                    so users see the same explanation on
                                    both surfaces. The trailing per-node
                                    precedence note lives in the meta
                                    pill below for at-a-glance reading. */}
                                <div className="run-settings-field-hint">
                                    Allow this agent to delegate complex
                                    sub-tasks to specialised subagents at
                                    run time. Recommended for open-ended,
                                    multi-step work.
                                </div>
                                <div className="run-settings-meta" role="note">
                                    <span
                                        className="run-settings-meta-icon"
                                        aria-hidden="true"
                                    >
                                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                            <circle cx="12" cy="12" r="10" />
                                            <line x1="12" y1="8"  x2="12" y2="12" />
                                            <line x1="12" y1="16" x2="12.01" y2="16" />
                                        </svg>
                                    </span>
                                    Per-node pins take precedence
                                </div>
                            </div>
                            <label
                                className="switch run-settings-switch"
                                aria-label="Use subagents for this run"
                            >
                                <input
                                    type="checkbox"
                                    checked={enabled}
                                    onChange={onToggleSwitch}
                                />
                                <span className="switch-track">
                                    <span className="switch-thumb" />
                                </span>
                            </label>
                        </div>
                    </div>

                    {/* Footer status bar — left side shows a live status
                        indicator (coloured dot + label), right side shows
                        "Auto-saved" so users know the choice persists. */}
                    <div className="run-settings-popover-footer">
                        <span className="run-settings-status">
                            <span
                                className={`run-settings-status-dot ${enabled ? 'run-settings-status-dot--on' : 'run-settings-status-dot--off'}`}
                                aria-hidden="true"
                            />
                            <span className="run-settings-status-label">
                                {enabled
                                    ? 'Subagents ON for next run'
                                    : 'Subagents OFF for next run'}
                            </span>
                        </span>
                        <span className="run-settings-saved">
                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <polyline points="20 6 9 17 4 12" />
                            </svg>
                            Auto-saved
                        </span>
                    </div>
                </div>
            )}
        </div>
    );
}

export default RunSettingsStrip;
