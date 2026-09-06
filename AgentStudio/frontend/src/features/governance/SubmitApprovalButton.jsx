// SPDX-License-Identifier: MIT
import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import useGovernanceStore from '../../store/governanceStore';

// Statuses for which submitting for approval makes sense. A brand-new artifact
// that governance hasn't seen yet reports null; a rejected one can be resubmitted.
const SUBMITTABLE = new Set([null, undefined, 'DRAFT', 'REJECTED', 'DEPRECATED']);

// Statuses in which the owner can still CANCEL the request (before an approver
// acts). Cancelling returns the artifact to an editable DRAFT.
const CANCELLABLE = new Set(['PENDING_APPROVAL', 'PENDING_L2']);

const BTN = {
    primary: {
        display: 'inline-flex', alignItems: 'center', gap: 6,
        padding: '5px 12px', borderRadius: 8, fontSize: 12, fontWeight: 600,
        color: '#fff', border: 'none', background: 'linear-gradient(135deg,#4f46e5,#7c3aed)',
    },
    ghost: {
        padding: '5px 12px', borderRadius: 8, fontSize: 12, fontWeight: 600,
        color: '#475569', border: '1px solid #e2e8f0', background: '#fff', cursor: 'pointer',
    },
};

/**
 * "Deploy" affordance.
 *
 * Deploying publishes the artifact as a reusable template for others, subject to
 * department-manager (HOD) approval. Click reveals a visibility selector
 * (public = all users, private = your department) plus an optional reason box,
 * then sends the request to the HOD — it lands in their ai-ui sidebar Inbox.
 * Renders nothing once the artifact is already pending/approved.
 *
 * Ownership:
 *   ``isOwner`` gates the two mutating actions this button exposes — Deploy
 *   (resubmit) and Cancel request (withdraw). A non-owner (e.g. an admin/HOD
 *   viewing a pending skill so they can approve it) sees no button at all;
 *   they act on the request from the Inbox tab instead. Default is ``true``
 *   to keep older call-sites that don't yet pass the flag working — those
 *   sites render the card only for the owner in practice.
 *
 * Props: entityType, name, isOwner?, onSubmitted?, onCancelled?, style
 */
export default function SubmitApprovalButton({ entityType, name, isOwner = true, onSubmitted, onCancelled, style }) {
    const submit = useGovernanceStore((s) => s.submit);
    const withdraw = useGovernanceStore((s) => s.withdraw);
    const fetchStatus = useGovernanceStore((s) => s.fetchStatus);
    const status = useGovernanceStore(
        (s) => (entityType && name ? s.statusMap[`${entityType}:${name}`] : undefined)
    );
    const [open, setOpen] = useState(false);
    const [reason, setReason] = useState('');
    const [visibility, setVisibility] = useState('public');
    const [busy, setBusy] = useState(false);
    const [cancelling, setCancelling] = useState(false);
    const [msg, setMsg] = useState(null);
    // True only for the brief window right after a submit, so we hide the
    // Deploy button while the pending status settles. A cancel does NOT set
    // this, so Deploy re-appears immediately after cancelling.
    const [justSubmitted, setJustSubmitted] = useState(false);

    useEffect(() => {
        if (entityType && name && status === undefined) fetchStatus(entityType, name);
    }, [entityType, name, status, fetchStatus]);

    async function handleSend(e) {
        e?.stopPropagation?.();
        if (busy || !name) return;
        setBusy(true);
        setMsg(null);
        try {
            const res = await submit(entityType, name, reason.trim(), visibility);
            // Close + confirm immediately; refresh the cached status in the
            // background so the UI doesn't block on a second round-trip.
            setOpen(false);
            setReason('');
            setJustSubmitted(true);
            // Surface the backend's "Submitted for approval to <names>" message
            // so the maker sees exactly who will review their deploy request.
            const approvers = Array.isArray(res?.approvers) ? res.approvers : [];
            const text = approvers.length > 0
                ? `Submitted for approval to: ${approvers.join(', ')}`
                : (res?.message || 'Deploy requested — pending manager approval');
            setMsg({ ok: true, text });
            onSubmitted?.();
            fetchStatus(entityType, name);
        } catch (err) {
            setMsg({ ok: false, text: err.message || 'Deploy failed' });
        } finally {
            setBusy(false);
        }
    }

    async function handleCancel(e) {
        e?.stopPropagation?.();
        if (cancelling || !name) return;
        setCancelling(true);
        setMsg(null);
        try {
            await withdraw(entityType, name);
            // Cancel returns the item to an editable DRAFT — clear the
            // "just submitted" flag so the Deploy button re-appears at once,
            // no navigation required.
            setJustSubmitted(false);
            setMsg({ ok: true, text: 'Deploy cancelled — this item is editable again' });
            onCancelled?.();
            fetchStatus(entityType, name);
        } catch (err) {
            setMsg({ ok: false, text: err.message || 'Cancel failed' });
        } finally {
            setCancelling(false);
        }
    }

    // Non-owners never see a mutating button — Deploy or Cancel are the
    // creator's responsibility. Admins/HODs viewing a pending item act on
    // it from the Inbox tab. This is a defence-in-depth check on top of the
    // backend owner guard on the corresponding endpoints.
    if (!isOwner) return null;

    // While the request is pending approval, offer the submitter a Cancel
    // affordance (the deploy is not yet final and can be withdrawn). The
    // button uses the same inline-flex row as Deploy so it stays aligned with
    // the surrounding header controls (badge, save status, mode toggle).
    // Keyboard events must also be stopped from bubbling: this button often
    // lives inside a role="button" card that treats Space / Enter as "open
    // detail". Without this, a user typing a space in the reason textarea
    // (or activating a scope pill with the keyboard) would inadvertently
    // trigger the parent card's activation handler.
    const _stopKeys = (e) => e.stopPropagation();

    if (CANCELLABLE.has(status ?? null)) {
        return (
            <span
                onClick={(e) => e.stopPropagation()}
                onKeyDown={_stopKeys}
                onKeyUp={_stopKeys}
                onKeyPress={_stopKeys}
                style={{ display: 'inline-flex', alignItems: 'center', gap: 8, ...style }}
            >
                <button
                    type="button"
                    style={{ ...BTN.ghost, display: 'inline-flex', alignItems: 'center', cursor: cancelling ? 'default' : 'pointer', opacity: cancelling ? 0.6 : 1 }}
                    disabled={cancelling}
                    onClick={handleCancel}
                    title="Cancel this deploy request and make the item editable again"
                >
                    {cancelling ? 'Cancelling…' : 'Cancel request'}
                </button>
            </span>
        );
    }

    // Hide once the artifact is in an approval-bearing state (unless we just
    // submitted and want to show the confirmation message).
    if (!msg && !SUBMITTABLE.has(status ?? null)) return null;

    const label = status === 'REJECTED' ? 'Redeploy' : 'Deploy';

    return (
        <span
            onClick={(e) => e.stopPropagation()}
            onKeyDown={_stopKeys}
            onKeyUp={_stopKeys}
            onKeyPress={_stopKeys}
            style={{ position: 'relative', display: 'inline-flex', alignItems: 'center', gap: 8, ...style }}
        >
            {/* Deploy stays visible whenever the item is submittable. We only
                hide it in the brief window right after a submit (justSubmitted)
                while the pending status settles — after a cancel it shows at
                once because justSubmitted was reset. */}
            {!justSubmitted && (
                <button type="button" style={{ ...BTN.primary, cursor: 'pointer' }} onClick={() => setOpen((v) => !v)}>
                    {label}
                </button>
            )}

            {open && (
                <DeployDialog
                    label={label}
                    entityType={entityType}
                    name={name}
                    visibility={visibility}
                    setVisibility={setVisibility}
                    reason={reason}
                    setReason={setReason}
                    busy={busy}
                    onSend={handleSend}
                    onClose={() => { if (!busy) { setOpen(false); setReason(''); } }}
                />
            )}

            {msg && (
                <span style={{ fontSize: 11, color: msg.ok ? '#15803d' : '#b91c1c' }}>{msg.text}</span>
            )}
        </span>
    );
}


// ── Deploy dialog ────────────────────────────────────────────────────────────
//
// Portal-mounted modal shown when the maker clicks Deploy / Redeploy on a
// skill (or any governed artifact). It replaces the earlier inline popover
// which, when the Skills tab held many rows, was clipped by adjacent cards
// and hard to read. A real modal renders above the card list, is scroll-
// isolated, keyboard-accessible (Escape closes), and gives the reason box
// plenty of room. Every event handler stops propagation because the modal
// is mounted via a portal outside of the SkillCard's role="button" wrapper
// — the wrapper won't see these events, but downstream global listeners
// (e.g. Zustand modals) still shouldn't be tripped by typing a space in
// the reason box.
function DeployDialog({
    label, entityType, name, visibility, setVisibility, reason, setReason,
    busy, onSend, onClose,
}) {
    // Escape closes, and while the dialog is open we lock body scroll so the
    // background list underneath can't jitter behind the overlay.
    useEffect(() => {
        const onKey = (e) => {
            if (e.key === 'Escape' && !busy) { e.stopPropagation(); onClose(); }
        };
        window.addEventListener('keydown', onKey, true);
        const prevOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        return () => {
            window.removeEventListener('keydown', onKey, true);
            document.body.style.overflow = prevOverflow;
        };
    }, [busy, onClose]);

    const _stopKeys = (e) => e.stopPropagation();
    const _entity = (entityType || '').toString().replace(/s$/, '') || 'artifact';
    const _entityLabel = _entity.charAt(0).toUpperCase() + _entity.slice(1);

    const dialog = (
        <div
            role="dialog"
            aria-modal="true"
            aria-label={`${label} ${_entityLabel}`}
            onClick={(e) => { e.stopPropagation(); onClose(); }}
            onKeyDown={_stopKeys}
            onKeyUp={_stopKeys}
            onKeyPress={_stopKeys}
            style={{
                position: 'fixed', inset: 0, zIndex: 10000,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                padding: 24,
                background: 'rgba(15, 23, 42, 0.45)',
                backdropFilter: 'blur(4px)',
                animation: 'fadeIn 0.18s ease',
            }}
        >
            <div
                onClick={(e) => e.stopPropagation()}
                onKeyDown={_stopKeys}
                onKeyUp={_stopKeys}
                onKeyPress={_stopKeys}
                style={{
                    display: 'flex', flexDirection: 'column',
                    width: '100%', maxWidth: 520, maxHeight: '82vh',
                    background: '#ffffff', border: '1px solid #e2e8f0',
                    borderRadius: 16, boxShadow: '0 24px 64px rgba(15, 23, 42, 0.18)',
                    overflow: 'hidden',
                }}
            >
                {/* Header */}
                <header style={{
                    display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
                    gap: 12, padding: '16px 20px 12px', borderBottom: '1px solid #e5eaf2',
                }}>
                    <div style={{ minWidth: 0 }}>
                        <h2 style={{
                            margin: 0, fontSize: 16, fontWeight: 700, color: '#0f172a',
                            letterSpacing: '-0.01em',
                        }}>
                            {label} {_entityLabel}
                        </h2>
                        <p style={{
                            margin: '4px 0 0', fontSize: 12.5, lineHeight: 1.45,
                            color: '#475569', fontWeight: 440, wordBreak: 'break-word',
                        }}>
                            <strong style={{ color: '#334155', fontWeight: 650 }}>{name}</strong> will be
                            sent for governance approval. It becomes usable once an approver signs off.
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        disabled={busy}
                        aria-label="Close"
                        style={{
                            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                            width: 28, height: 28, flexShrink: 0, borderRadius: 8,
                            border: '1px solid #e2e8f0', background: '#fff', color: '#64748b',
                            cursor: busy ? 'not-allowed' : 'pointer',
                        }}
                    >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                            <line x1="18" y1="6" x2="6" y2="18" />
                            <line x1="6" y1="6" x2="18" y2="18" />
                        </svg>
                    </button>
                </header>

                {/* Body */}
                <div style={{
                    flex: 1, overflow: 'auto',
                    padding: '16px 20px 12px',
                    display: 'flex', flexDirection: 'column', gap: 16,
                }}>
                    {/* Visibility */}
                    <div>
                        <div style={{
                            fontSize: 11.5, fontWeight: 700, color: '#334155',
                            marginBottom: 8, letterSpacing: '0.03em', textTransform: 'uppercase',
                        }}>
                            Visibility once approved
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                            {[
                                ['public',  'Public',      'All users can use this once approved'],
                                ['private', 'Department',  'Only users in your department'],
                            ].map(([val, title, sub]) => {
                                const active = visibility === val;
                                return (
                                    <button
                                        key={val}
                                        type="button"
                                        onClick={() => setVisibility(val)}
                                        disabled={busy}
                                        onKeyDown={_stopKeys}
                                        onKeyUp={_stopKeys}
                                        onKeyPress={_stopKeys}
                                        style={{
                                            display: 'flex', flexDirection: 'column', alignItems: 'flex-start',
                                            gap: 4, padding: '12px 14px', borderRadius: 12, textAlign: 'left',
                                            cursor: busy ? 'not-allowed' : 'pointer',
                                            border: `1.5px solid ${active ? '#7c3aed' : '#e2e8f0'}`,
                                            background: active ? '#f5f3ff' : '#fff',
                                            color: active ? '#6d28d9' : '#334155',
                                            transition: 'border-color 140ms ease, background 140ms ease',
                                        }}
                                    >
                                        <span style={{ fontSize: 13, fontWeight: 700 }}>{title}</span>
                                        <span style={{ fontSize: 11.5, fontWeight: 440, color: active ? '#7c3aed' : '#64748b' }}>
                                            {sub}
                                        </span>
                                    </button>
                                );
                            })}
                        </div>
                    </div>

                    {/* Reason */}
                    <div>
                        <div style={{
                            fontSize: 11.5, fontWeight: 700, color: '#334155',
                            marginBottom: 8, letterSpacing: '0.03em', textTransform: 'uppercase',
                        }}>
                            Reason for approval{' '}
                            <span style={{
                                fontWeight: 500, color: '#94a3b8',
                                letterSpacing: 0, textTransform: 'none',
                            }}>
                                (optional)
                            </span>
                        </div>
                        <textarea
                            autoFocus
                            value={reason}
                            onChange={(e) => setReason(e.target.value)}
                            onKeyDown={_stopKeys}
                            onKeyUp={_stopKeys}
                            onKeyPress={_stopKeys}
                            placeholder="Why should this be approved? What does it do?"
                            rows={4}
                            disabled={busy}
                            style={{
                                width: '100%', boxSizing: 'border-box',
                                resize: 'vertical', minHeight: 96,
                                padding: '10px 12px', border: '1px solid #e2e8f0', borderRadius: 10,
                                fontSize: 13, lineHeight: 1.5, color: '#0f172a',
                                background: busy ? '#f8fafc' : '#fff',
                                outline: 'none', fontFamily: 'inherit',
                            }}
                        />
                    </div>
                </div>

                {/* Footer */}
                <div style={{
                    display: 'flex', justifyContent: 'flex-end', gap: 10,
                    padding: '12px 20px 16px', borderTop: '1px solid #eef2f7',
                }}>
                    <button
                        type="button"
                        onClick={onClose}
                        disabled={busy}
                        style={{
                            height: 36, padding: '0 16px', borderRadius: 11,
                            border: '1px solid #e2e8f0', background: '#fff', color: '#475569',
                            fontSize: 13, fontWeight: 650,
                            cursor: busy ? 'not-allowed' : 'pointer',
                        }}
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        onClick={onSend}
                        disabled={busy}
                        style={{
                            display: 'inline-flex', alignItems: 'center', gap: 8,
                            minWidth: 148, justifyContent: 'center',
                            height: 36, padding: '0 18px', borderRadius: 11, border: 'none',
                            background: 'linear-gradient(135deg, #4f46e5, #7c3aed)', color: '#fff',
                            fontSize: 13, fontWeight: 650,
                            cursor: busy ? 'not-allowed' : 'pointer',
                            opacity: busy ? 0.7 : 1,
                            boxShadow: '0 1px 2px rgba(15, 23, 42, 0.06), 0 8px 20px rgba(79, 70, 229, 0.22)',
                        }}
                    >
                        {busy ? (
                            <>
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" style={{ animation: 'spin 0.9s linear infinite' }}>
                                    <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                                </svg>
                                Requesting…
                            </>
                        ) : (
                            `Request ${label}`
                        )}
                    </button>
                </div>
            </div>
        </div>
    );

    // Portal so we escape the SkillCard's stacking context / overflow clipping.
    return createPortal(dialog, document.body);
}
