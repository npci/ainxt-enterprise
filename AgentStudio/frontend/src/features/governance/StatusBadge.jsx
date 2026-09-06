// SPDX-License-Identifier: MIT
import { useEffect } from 'react';
import useGovernanceStore from '../../store/governanceStore';

// Statuses that are still "in flight" — worth polling for a resolution
// (e.g. an approver acting in the ai-ui inbox, a separate app).
const PENDING_STATES = new Set(['PENDING_APPROVAL', 'PENDING_L2']);

// Visual mapping for every governance lifecycle status. Self-contained inline
// styles so this drops into any card/list without depending on ABStudio's CSS.
const STATUS_STYLE = {
    PENDING_APPROVAL: { label: 'Awaiting Approval', bg: '#fef3c7', fg: '#b45309', bd: '#fde68a', dot: '#f59e0b' },
    PENDING_L2:       { label: 'Awaiting L2',        bg: '#fef3c7', fg: '#b45309', bd: '#fde68a', dot: '#f59e0b' },
    APPROVED:         { label: 'Approved',           bg: '#dbeafe', fg: '#1d4ed8', bd: '#bfdbfe', dot: '#3b82f6' },
    PRODUCTION:       { label: 'Live',               bg: '#dcfce7', fg: '#15803d', bd: '#bbf7d0', dot: '#22c55e' },
    ACTIVE:           { label: 'Live',               bg: '#dcfce7', fg: '#15803d', bd: '#bbf7d0', dot: '#22c55e' },
    REJECTED:         { label: 'Rejected',           bg: '#fee2e2', fg: '#b91c1c', bd: '#fecaca', dot: '#ef4444' },
    DEPRECATED:       { label: 'Deprecated',         bg: '#f3f4f6', fg: '#6b7280', bd: '#e5e7eb', dot: '#9ca3af' },
    DRAFT:            { label: 'Not Approved',       bg: '#f3f4f6', fg: '#6b7280', bd: '#e5e7eb', dot: '#9ca3af' },
    // Fetched but no governance record — never submitted, so it cannot run.
    NOT_SUBMITTED:    { label: 'Not Submitted',      bg: '#f3f4f6', fg: '#6b7280', bd: '#e5e7eb', dot: '#9ca3af' },
};

/**
 * Governance status pill.
 *
 * Pass an explicit ``status`` (when the parent already has it), or pass
 * ``entityType`` + ``name`` to have the badge fetch + cache the status itself.
 * Renders nothing while the status is unknown so it never disrupts layout.
 *
 * ``poll``: when true (default), the badge re-fetches on mount and, while the
 * status is still pending, polls every 15s so an approval performed elsewhere
 * (the ai-ui sidebar Inbox is a separate app) reflects here without a reload.
 * Card grids can pass ``poll={false}`` to avoid N background timers.
 */
export default function StatusBadge({ status, entityType, name, style, poll = true }) {
    const cached = useGovernanceStore(
        (s) => (entityType && name ? s.statusMap[`${entityType}:${name}`] : undefined)
    );
    const fetchStatus = useGovernanceStore((s) => s.fetchStatus);

    // Distinguish "still loading" (undefined) from "fetched, no record" (null).
    // A self-managed badge that has resolved to null means the artifact was
    // never submitted — show a "Not Submitted" pill so it's clear it can't run.
    let effective = status || cached;
    if (!effective && !status && entityType && name && cached === null) {
        effective = 'NOT_SUBMITTED';
    }

    // Refresh on mount (so reopening the editor never shows a stale cache) and,
    // if self-managed, poll while the status is unresolved.
    useEffect(() => {
        if (status || !entityType || !name) return;
        fetchStatus(entityType, name);   // always refresh on (re)mount
        if (!poll) return;
        const id = setInterval(() => {
            const cur = useGovernanceStore.getState().statusMap[`${entityType}:${name}`];
            if (cur && !PENDING_STATES.has(cur)) return; // resolved — stop hitting the API
            fetchStatus(entityType, name);
        }, 15000);
        return () => clearInterval(id);
    }, [status, entityType, name, poll, fetchStatus]);

    if (!effective) return null;

    const cfg = STATUS_STYLE[effective] || STATUS_STYLE.DRAFT;

    return (
        <span
            title={`Governance status: ${cfg.label}`}
            style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                padding: '2px 8px',
                borderRadius: 999,
                fontSize: 11,
                fontWeight: 600,
                lineHeight: 1.4,
                background: cfg.bg,
                color: cfg.fg,
                border: `1px solid ${cfg.bd}`,
                whiteSpace: 'nowrap',
                ...style,
            }}
        >
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: cfg.dot }} />
            {cfg.label}
        </span>
    );
}

export { STATUS_STYLE };
