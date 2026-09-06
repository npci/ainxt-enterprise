// SPDX-License-Identifier: MIT
import { useEffect, useState, useCallback } from 'react';
import TriggerPicker, { describeSchedule } from './TriggerPicker';
import useTriggersStore from '../../store/triggersStore';
import { formatIst, durationLabel } from './triggerUtils';

// Threshold below which we surface a "this trigger is about to fire — really
// edit?" confirmation. In milliseconds so we can compare directly against
// ``next_run_at - now``. Anything sooner than this is considered "imminent"
// and worth double-checking with the user.
const IMMINENT_FIRE_WINDOW_MS = 5 * 60 * 1000;

/**
 * Explicit "Edit" button that opens the schedule editor. Replaces the earlier
 * chevron so the interaction is discoverable and the intent unambiguous —
 * clicking edits, everything else on the card leaves the schedule alone.
 * The pencil SVG is a common editor affordance in this UI already.
 */
function EditButton({ onClick, label }) {
    return (
        <button
            type="button"
            className="trigger-card-edit-btn"
            onClick={onClick}
            aria-label={label}
            title={label}
        >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 20h9" />
                <path d="M16.5 3.5a2.121 2.121 0 1 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
            </svg>
            <span>Edit</span>
        </button>
    );
}

/**
 * Confirmation dialog surfaced when the user clicks Edit within
 * ``IMMINENT_FIRE_WINDOW_MS`` of the trigger's next scheduled run. Gives them
 * a chance to cancel out (leave the schedule untouched) or continue into the
 * editor. We do NOT auto-block editing — the trigger scheduler already
 * reloads the row fresh on every fire (see trigger_scheduler.py:_fire_trigger),
 * so a race between an in-progress edit and the fire is safe. This is a UX
 * safeguard against accidental late edits, not a correctness gate.
 */
function ImminentFireConfirm({ open, minutesRemaining, onConfirm, onCancel }) {
    if (!open) return null;
    // Human-friendly countdown label ("in 42 seconds" / "in 3 minutes").
    const label = minutesRemaining < 1
        ? 'in less than a minute'
        : `in ~${Math.round(minutesRemaining)} minute${Math.round(minutesRemaining) === 1 ? '' : 's'}`;
    return (
        <div
            className="trigger-imminent-overlay"
            role="dialog"
            aria-modal="true"
            aria-labelledby="trigger-imminent-title"
            onClick={onCancel}
        >
            <div className="trigger-imminent-modal" onClick={(e) => e.stopPropagation()}>
                <h3 id="trigger-imminent-title">Trigger about to fire</h3>
                <p>
                    This trigger is already scheduled and is set to run{' '}
                    <strong>{label}</strong>. Do you still want to modify the timing, or cancel and let it run as scheduled?
                </p>
                <div className="trigger-imminent-actions">
                    <button
                        type="button"
                        className="trigger-imminent-cancel"
                        onClick={onCancel}
                    >
                        Cancel — leave as is
                    </button>
                    <button
                        type="button"
                        className="trigger-imminent-confirm"
                        onClick={onConfirm}
                    >
                        Modify anyway
                    </button>
                </div>
            </div>
        </div>
    );
}

/**
 * TriggerSection — drop-in "Routines" panel that hangs off either an Agent
 * or a workflow's Agent node. Loads existing triggers from the backend,
 * lets the user add/edit/delete, and persists every change immediately.
 *
 * Required props:
 *   targetKind: 'workflow' | 'agent'
 *   targetId:   the parent record id (must already be saved on the backend)
 *
 * Optional:
 *   nodeId:     when set (workflow case), scopes triggers to a specific
 *               agent node inside the workflow. Each node sees only its own
 *               triggers; the scheduler runs the chain starting from this
 *               node so its output flows to the downstream nodes.
 *   disabled:   string ("Save first to set a trigger") — disables the section
 *   variant:    'compact' (ConfigPanel) | 'card' (AgentEditor) |
 *               'modal'   (TriggerModal — suppresses the duplicate "Triggers"
 *                          heading because the modal header already provides it)
 */
function TriggerSection({ targetKind, targetId, nodeId = null, disabled, variant = 'compact' }) {
    const triggersByTarget   = useTriggersStore((s) => s.triggersByTarget);
    const loadTriggersFor    = useTriggersStore((s) => s.loadTriggersFor);
    const createTriggerStore = useTriggersStore((s) => s.createTrigger);
    const updateTriggerStore = useTriggersStore((s) => s.updateTrigger);
    const deleteTriggerStore = useTriggersStore((s) => s.deleteTrigger);
    const loadHistory        = useTriggersStore((s) => s.loadHistory);
    const historyByTrigger   = useTriggersStore((s) => s.historyByTrigger);

    const key = targetId ? `${targetKind}:${targetId}:${nodeId || ''}` : null;
    const triggers = key ? (triggersByTarget[key] || []) : [];

    const [draftSchedule, setDraftSchedule] = useState(null);
    const [draftInput, setDraftInput] = useState('');
    const [draftName, setDraftName] = useState('');
    const [openHistoryFor, setOpenHistoryFor] = useState(null);
    const [error, setError] = useState('');
    const [saving, setSaving] = useState(false);
    // Saved triggers render collapsed by default: the header row shows name,
    // schedule summary, enabled toggle, History, Delete and an explicit
    // Edit button. Clicking Edit either opens the schedule editor directly
    // (fires > 5 min away or no next-run at all) or first raises the
    // ImminentFireConfirm dialog (fires < 5 min away, so the user knows the
    // trigger is about to run and can choose to cancel out).
    // Draft cards ignore this state and are always expanded so the user can
    // fill them in.
    const [expandedIds, setExpandedIds] = useState(() => new Set());
    // trigger id that is awaiting the "about to fire — really edit?"
    // confirmation. Cleared on either confirm or cancel.
    const [confirmingEdit, setConfirmingEdit] = useState(null);

    // Compute minutes-until-fire for a trigger. Returns Infinity if there is
    // no scheduled next run (paused / no-next-run / event-driven triggers).
    // Kept as a pure helper so it can be reused inside the confirm dialog.
    const minutesUntilFire = useCallback((trigger) => {
        if (!trigger || !trigger.next_run_at) return Infinity;
        const t = Date.parse(trigger.next_run_at);
        if (Number.isNaN(t)) return Infinity;
        return Math.max(0, (t - Date.now()) / 60000);
    }, []);

    // Opens the editor for the given trigger, showing the imminent-fire
    // warning first when applicable. Split out so the header Edit button and
    // any future entry point (e.g. keyboard shortcut) share the same policy.
    const openEditorFor = useCallback((trigger) => {
        const minsLeft = minutesUntilFire(trigger);
        const nearFire = Number.isFinite(minsLeft) && minsLeft * 60000 < IMMINENT_FIRE_WINDOW_MS;
        // Skip the warning when the card is already expanded — user is asking
        // to close the editor, not to start a fresh edit session.
        if (expandedIds.has(trigger.id)) {
            setExpandedIds((prev) => {
                const next = new Set(prev);
                next.delete(trigger.id);
                return next;
            });
            return;
        }
        if (nearFire) {
            setConfirmingEdit(trigger.id);
            return;
        }
        setExpandedIds((prev) => new Set(prev).add(trigger.id));
    }, [expandedIds, minutesUntilFire]);

    const confirmImminentEdit = useCallback(() => {
        if (!confirmingEdit) return;
        setExpandedIds((prev) => new Set(prev).add(confirmingEdit));
        setConfirmingEdit(null);
    }, [confirmingEdit]);

    const cancelImminentEdit = useCallback(() => {
        setConfirmingEdit(null);
    }, []);

    useEffect(() => {
        if (!targetId || disabled) return;
        loadTriggersFor(targetKind, targetId, nodeId);
    }, [targetKind, targetId, nodeId, disabled, loadTriggersFor]);

    const handleAddDraft = () => {
        setError('');
        setDraftSchedule({ type: 'daily', at_time: '18:00' });
        setDraftInput('');
        setDraftName('');
    };

    const handleCancelDraft = () => {
        setDraftSchedule(null);
        setError('');
    };

    const handleSaveDraft = async () => {
        if (!draftSchedule) return;
        setSaving(true);
        setError('');
        try {
            const created = await createTriggerStore({
                targetKind,
                targetId,
                nodeId,
                name: draftName,
                schedule: draftSchedule,
                inputText: draftInput,
                enabled: true,
            });
            if (created) {
                setDraftSchedule(null);
                setDraftInput('');
                setDraftName('');
            } else {
                setError('Could not save trigger. Check the backend logs.');
            }
        } finally {
            setSaving(false);
        }
    };

    const handleEditExisting = useCallback(
        async (trigger, nextSchedule) => {
            await updateTriggerStore(trigger.id, { schedule: nextSchedule }, targetKind, targetId, nodeId);
        },
        [updateTriggerStore, targetKind, targetId, nodeId],
    );

    const handleEditInput = useCallback(
        async (trigger, nextInput) => {
            await updateTriggerStore(trigger.id, { input_text: nextInput }, targetKind, targetId, nodeId);
        },
        [updateTriggerStore, targetKind, targetId, nodeId],
    );

    const handleEditName = useCallback(
        async (trigger, nextName) => {
            await updateTriggerStore(trigger.id, { name: nextName }, targetKind, targetId, nodeId);
        },
        [updateTriggerStore, targetKind, targetId, nodeId],
    );

    const handleToggle = useCallback(
        async (trigger) => {
            await updateTriggerStore(
                trigger.id,
                { enabled: !trigger.enabled },
                targetKind,
                targetId,
                nodeId,
            );
        },
        [updateTriggerStore, targetKind, targetId, nodeId],
    );

    const handleDelete = useCallback(
        async (trigger) => {
            await deleteTriggerStore(trigger.id, targetKind, targetId, nodeId);
            if (openHistoryFor === trigger.id) setOpenHistoryFor(null);
        },
        [deleteTriggerStore, targetKind, targetId, nodeId, openHistoryFor],
    );

    const handleToggleHistory = useCallback(
        async (trigger) => {
            if (openHistoryFor === trigger.id) {
                setOpenHistoryFor(null);
                return;
            }
            await loadHistory(trigger.id);
            setOpenHistoryFor(trigger.id);
        },
        [openHistoryFor, loadHistory],
    );

    const sectionClass = variant === 'card'
        ? 'agent-config-section trigger-section trigger-section--card'
        : variant === 'modal'
            ? 'trigger-section trigger-section--modal'
            : 'config-section trigger-section trigger-section--compact';

    const titleClass = variant === 'card' ? 'agent-config-section-title' : 'form-divider';

    // When mounted inside TriggerModal the modal header already shows the
    // "Triggers — <name>" title and IST subtitle, so the inner heading
    // would just duplicate it and waste vertical space.
    const showHeading = variant !== 'modal';

    return (
        <section className={sectionClass}>
            {showHeading && (
                <div className="trigger-section-heading">
                    <h2 className={titleClass}>Triggers</h2>
                    <p className="trigger-section-hint">
                        Run this {targetKind === 'workflow' ? 'workflow' : 'agent'} automatically on a schedule (IST).
                    </p>
                </div>
            )}

            {disabled ? (
                <p className="trigger-section-disabled">{disabled}</p>
            ) : (
                <>
                    {triggers.length === 0 && !draftSchedule && (
                        <p className="trigger-section-empty">No triggers yet. Add one to run on a schedule.</p>
                    )}

                    {triggers.map((t) => {
                        const isExpanded = expandedIds.has(t.id);
                        return (
                        <div key={t.id} className={`trigger-card ${!t.enabled ? 'is-disabled' : ''} ${isExpanded ? 'is-expanded' : 'is-collapsed'}`}>
                            <div className="trigger-card-header">
                                <input
                                    type="text"
                                    className="trigger-card-name"
                                    placeholder="Trigger name (optional)"
                                    defaultValue={t.name || ''}
                                    onBlur={(e) => {
                                        const v = e.target.value;
                                        if (v !== (t.name || '')) handleEditName(t, v);
                                    }}
                                />
                                <label className="trigger-card-toggle" title={t.enabled ? 'Enabled' : 'Paused'}>
                                    <input
                                        type="checkbox"
                                        checked={!!t.enabled}
                                        onChange={() => handleToggle(t)}
                                    />
                                    <span>{t.enabled ? 'Enabled' : 'Paused'}</span>
                                </label>
                                <button
                                    type="button"
                                    className="trigger-card-history-btn"
                                    onClick={() => handleToggleHistory(t)}
                                    title="View execution history"
                                >
                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                        <polyline points="1 4 1 10 7 10" />
                                        <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
                                    </svg>
                                    History
                                </button>
                                <button
                                    type="button"
                                    className="trigger-card-delete"
                                    onClick={() => handleDelete(t)}
                                    aria-label="Delete trigger"
                                    title="Delete trigger"
                                >
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                        <polyline points="3 6 5 6 21 6" />
                                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                                    </svg>
                                </button>
                                <EditButton
                                    onClick={() => openEditorFor(t)}
                                    label={isExpanded ? 'Close editor' : 'Edit trigger timing'}
                                />
                            </div>
                            <ImminentFireConfirm
                                open={confirmingEdit === t.id}
                                minutesRemaining={minutesUntilFire(t)}
                                onConfirm={confirmImminentEdit}
                                onCancel={cancelImminentEdit}
                            />

                            {/* Collapsed summary: schedule sentence + next-run
                                so the user can see the key info at a glance
                                without having to expand every card. */}
                            {!isExpanded && (
                                <div className="trigger-card-collapsed-summary">
                                    <span className="trigger-card-collapsed-schedule">
                                        {describeSchedule(t.schedule)}
                                    </span>
                                    {t.next_run_at && (
                                        <span className="trigger-card-collapsed-next">
                                            Next: <strong>{formatIst(t.next_run_at)}</strong>
                                        </span>
                                    )}
                                </div>
                            )}

                            {isExpanded && (
                                <>
                                    <TriggerPicker
                                        schedule={t.schedule}
                                        onChange={(next) => handleEditExisting(t, next)}
                                    />

                                    <label className="trigger-card-input-label">
                                        <span>Input message sent to the {targetKind} when the trigger fires</span>
                                        <textarea
                                            className="trigger-card-input"
                                            rows={2}
                                            placeholder="e.g. Run the daily report and email it to me"
                                            defaultValue={t.input_text || ''}
                                            onBlur={(e) => {
                                                const v = e.target.value;
                                                if (v !== (t.input_text || '')) handleEditInput(t, v);
                                            }}
                                        />
                                    </label>

                                    <div className="trigger-card-meta">
                                        {t.next_run_at && (
                                            <span>Next run: <strong>{formatIst(t.next_run_at)}</strong></span>
                                        )}
                                        {t.last_run_at && (
                                            <span>
                                                Last: <strong className={`status-${t.last_status || ''}`}>
                                                    {t.last_status || 'pending'}
                                                </strong>{' '}
                                                at {formatIst(t.last_run_at)}
                                            </span>
                                        )}
                                    </div>
                                </>
                            )}

                            {openHistoryFor === t.id && (
                                <ExecutionHistory rows={historyByTrigger[t.id] || []} />
                            )}
                        </div>
                        );
                    })}

                    {draftSchedule && (
                        <div className="trigger-card is-draft">
                            <div className="trigger-card-header">
                                <input
                                    type="text"
                                    className="trigger-card-name"
                                    placeholder="Trigger name (optional)"
                                    value={draftName}
                                    onChange={(e) => setDraftName(e.target.value)}
                                />
                            </div>
                            <TriggerPicker
                                schedule={draftSchedule}
                                onChange={setDraftSchedule}
                            />
                            <label className="trigger-card-input-label">
                                <span>Input message sent to the {targetKind} when the trigger fires</span>
                                <textarea
                                    className="trigger-card-input"
                                    rows={2}
                                    placeholder="e.g. Run the daily report and email it to me"
                                    value={draftInput}
                                    onChange={(e) => setDraftInput(e.target.value)}
                                />
                            </label>
                            {error && <div className="trigger-card-error">{error}</div>}
                            <div className="trigger-draft-actions">
                                <button
                                    type="button"
                                    className="trigger-draft-cancel"
                                    onClick={handleCancelDraft}
                                    disabled={saving}
                                >
                                    Cancel
                                </button>
                                <button
                                    type="button"
                                    className="trigger-draft-save"
                                    onClick={handleSaveDraft}
                                    disabled={saving}
                                >
                                    {saving ? 'Saving…' : 'Save trigger'}
                                </button>
                            </div>
                        </div>
                    )}

                    {!draftSchedule && (
                        <button
                            type="button"
                            className="trigger-add-btn"
                            onClick={handleAddDraft}
                        >
                            + Add {triggers.length > 0 ? 'another' : 'a'} trigger
                        </button>
                    )}
                </>
            )}
        </section>
    );
}

function ExecutionHistory({ rows }) {
    if (!rows || rows.length === 0) {
        return <p className="trigger-history-empty">No runs yet.</p>;
    }
    return (
        <div className="trigger-history">
            {rows.map((r) => (
                <details key={r.id} className={`trigger-history-row status-${r.status}`}>
                    <summary>
                        <span className={`trigger-history-status status-${r.status}`}>
                            {r.status === 'success' ? '✓' : r.status === 'error' ? '✕' : '…'}
                        </span>
                        <span>{formatIst(r.started_at)}</span>
                        {r.finished_at && (
                            <span className="trigger-history-duration">
                                · {durationLabel(r.started_at, r.finished_at)}
                            </span>
                        )}
                    </summary>
                    <div className="trigger-history-body">
                        {r.input_text && (
                            <>
                                <div className="trigger-history-label">Input</div>
                                <pre className="trigger-history-pre">{r.input_text}</pre>
                            </>
                        )}
                        {r.output && (
                            <>
                                <div className="trigger-history-label">Output</div>
                                <pre className="trigger-history-pre">{r.output}</pre>
                            </>
                        )}
                        {r.error && (
                            <>
                                <div className="trigger-history-label">Error</div>
                                <pre className="trigger-history-pre trigger-history-error">{r.error}</pre>
                            </>
                        )}
                    </div>
                </details>
            ))}
        </div>
    );
}

export default TriggerSection;
