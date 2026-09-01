// SPDX-License-Identifier: Apache-2.0
import { create } from 'zustand';
import { apiFetch } from '../config/api';

// Governance / approval layer store (Build Studio side).
//
// Status + submit go through ABStudio's OWN backend (apiFetch → /ainxt/v1/api/abs),
// which builds the platform governance mirror record on demand from the
// artifact's real data. (Calling the platform governance router directly 404s
// for artifacts that don't yet have a *_pg mirror record.) Approval REVIEW
// still happens in the ai-ui sidebar Inbox (governance_approval items).
//   GET  /governance-status/{type}/{name}  -> { status }
//   POST /governance-submit/{type}/{name}  -> { status }

const useGovernanceStore = create((set) => ({
    statusMap: {},          // `${type}:${name}` -> status (null = not submitted)

    // Status lookup for a single artifact (cached in statusMap so a dashboard
    // of N cards doesn't re-request the same entity).
    fetchStatus: async (entityType, name) => {
        if (!name) return null;
        const key = `${entityType}:${name}`;
        let status = null;
        try {
            const data = await apiFetch(
                `/governance-status/${entityType}/${encodeURIComponent(name)}`
            );
            status = data?.status ?? null;
        } catch {
            status = null;   // treat any failure as "not submitted yet"
        }
        set((s) => (s.statusMap[key] === status
            ? s
            : { statusMap: { ...s.statusMap, [key]: status } }));
        return status;
    },

    // Send an artifact to its department manager (HOD) to be published as a
    // shared template ("Deploy"). The request lands in the manager's ai-ui
    // sidebar Inbox as a ``governance_approval`` item. ``reason`` is an optional
    // submitter note; ``visibility`` ('public'|'private') is the requested
    // catalog visibility applied to the template on approval.
    submit: (entityType, name, reason = '', visibility = 'public') =>
        apiFetch(`/governance-submit/${entityType}/${encodeURIComponent(name)}`,
            { method: 'POST', body: JSON.stringify({ reason: reason || '', visibility }) }),

    // Cancel a pending deploy request ("Cancel"). Returns the artifact to an
    // editable DRAFT so the owner can keep working on it (or re-deploy later).
    // Optimistically flips the cached status so the UI unlocks immediately;
    // the real status is confirmed by the caller's follow-up fetchStatus.
    withdraw: async (entityType, name) => {
        const data = await apiFetch(
            `/governance-withdraw/${entityType}/${encodeURIComponent(name)}`,
            { method: 'POST' },
        );
        const key = `${entityType}:${name}`;
        const status = data?.status ?? 'DRAFT';
        set((s) => ({ statusMap: { ...s.statusMap, [key]: status } }));
        return status;
    },
}));

export default useGovernanceStore;
