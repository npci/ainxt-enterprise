// SPDX-License-Identifier: Apache-2.0
import { create } from 'zustand';
import { apiFetch as _apiFetch } from '../config/api';

/**
 * Triggers store — Routines / scheduled execution of workflows and agents.
 *
 * Shape mirrors agentsStore: load, create, update, delete + light caching.
 * Notifications (recent unseen executions) live in a separate slice so a
 * background poller can keep them fresh without re-fetching every trigger.
 *
 * All times round-trip in IST (server-side); we display them as-is.
 */

// Trigger operations can be slower than regular CRUD — use a 10 s timeout.
const apiFetch = (path, options) => _apiFetch(path, options, 10_000);

const useTriggersStore = create((set, get) => ({
    // Map of `${kind}:${id}` -> array of triggers attached to that target.
    triggersByTarget: {},
    isLoading: false,
    error: null,

    // Notification slice. `recentExecutions` is the full ordered list (latest
    // first) — kept resident so the bell stays scrollable history, not a
    // one-shot inbox. `unseenCount` drives the red badge.
    recentExecutions: [],
    unseenCount: 0,
    historyByTrigger: {}, // trigger_id -> execution[]

    // For workflow nodes we cache triggers per-node so different agent nodes
    // can show different lists. The cache key encodes the node id (empty
    // string for "no node" / agent targets).
    loadTriggersFor: async (targetKind, targetId, nodeId = null) => {
        if (!targetKind || !targetId) return [];
        const key = `${targetKind}:${targetId}:${nodeId || ''}`;
        set({ isLoading: true, error: null });
        try {
            const params = new URLSearchParams({ target_kind: targetKind, target_id: targetId });
            if (nodeId) {
                params.set('node_id', nodeId);
                params.set('node_scope', 'exact');
            } else if (targetKind === 'workflow') {
                // Workflow-wide triggers (no node bound). Avoids mixing in
                // other agent nodes' triggers when the user clicks on the
                // workflow-level canvas chrome.
                params.set('node_scope', 'workflow_only');
            }
            const data = await apiFetch(`/triggers?${params}`);
            set((state) => ({
                triggersByTarget: { ...state.triggersByTarget, [key]: data || [] },
                isLoading: false,
            }));
            return data || [];
        } catch (error) {
            set({ error: error.message, isLoading: false });
            return [];
        }
    },

    createTrigger: async ({ targetKind, targetId, nodeId = null, name, schedule, inputText, enabled = true }) => {
        try {
            const trigger = await apiFetch('/triggers', {
                method: 'POST',
                body: JSON.stringify({
                    target_kind: targetKind,
                    target_id:   targetId,
                    node_id:     nodeId || null,
                    name:        name || '',
                    schedule,
                    input_text:  inputText || '',
                    enabled,
                }),
            });
            const key = `${targetKind}:${targetId}:${nodeId || ''}`;
            set((state) => {
                const existing = state.triggersByTarget[key] || [];
                return {
                    triggersByTarget: {
                        ...state.triggersByTarget,
                        [key]: [trigger, ...existing],
                    },
                };
            });
            return trigger;
        } catch (error) {
            set({ error: error.message });
            return null;
        }
    },

    updateTrigger: async (id, payload, targetKind, targetId, nodeId = null) => {
        try {
            const trigger = await apiFetch(`/triggers/${id}`, {
                method: 'PUT',
                body: JSON.stringify(payload),
            });
            const key = `${targetKind}:${targetId}:${nodeId || ''}`;
            set((state) => ({
                triggersByTarget: {
                    ...state.triggersByTarget,
                    [key]: (state.triggersByTarget[key] || []).map((t) =>
                        t.id === id ? trigger : t,
                    ),
                },
            }));
            return trigger;
        } catch (error) {
            set({ error: error.message });
            return null;
        }
    },

    deleteTrigger: async (id, targetKind, targetId, nodeId = null) => {
        try {
            await apiFetch(`/triggers/${id}`, { method: 'DELETE' });
            const key = `${targetKind}:${targetId}:${nodeId || ''}`;
            set((state) => ({
                triggersByTarget: {
                    ...state.triggersByTarget,
                    [key]: (state.triggersByTarget[key] || []).filter((t) => t.id !== id),
                },
            }));
            return true;
        } catch (error) {
            set({ error: error.message });
            return false;
        }
    },

    // --- Notifications -----------------------------------------------------
    //
    // The bell shows the most recent runs from the `trigger_executions` table
    // (newest first). Each row carries its own `seen` flag — used to drive
    // the unread badge, NOT to filter the list. Clicking an item or pressing
    // "Mark all read" only flips the flag, the row stays visible so the
    // user can re-open it and copy past input/output.

    loadNotifications: async () => {
        try {
            const params = new URLSearchParams({ limit: '50' });
            const data = await apiFetch(`/trigger-executions?${params}`);
            const list = Array.isArray(data) ? data : [];
            set({
                recentExecutions: list,
                unseenCount: list.filter((r) => !r.seen).length,
            });
            return list;
        } catch {
            return [];
        }
    },

    markSeen: async (executionId) => {
        try {
            await apiFetch(`/trigger-executions/${executionId}/seen`, { method: 'POST' });
            set((state) => {
                const next = state.recentExecutions.map((r) =>
                    r.id === executionId ? { ...r, seen: true } : r,
                );
                return {
                    recentExecutions: next,
                    unseenCount: next.filter((r) => !r.seen).length,
                };
            });
        } catch {
            // ignore
        }
    },

    markAllSeen: async () => {
        try {
            await apiFetch('/trigger-executions/mark-all-seen', { method: 'POST' });
            set((state) => ({
                recentExecutions: state.recentExecutions.map((r) => ({ ...r, seen: true })),
                unseenCount: 0,
            }));
        } catch {
            // ignore
        }
    },

    deleteExecution: async (executionId) => {
        // Optimistic — drop the row immediately so the bell feels snappy.
        let removed;
        set((state) => {
            removed = state.recentExecutions.find((r) => r.id === executionId);
            const next = state.recentExecutions.filter((r) => r.id !== executionId);
            return {
                recentExecutions: next,
                unseenCount: next.filter((r) => !r.seen).length,
            };
        });
        try {
            await apiFetch(`/trigger-executions/${executionId}`, { method: 'DELETE' });
            return true;
        } catch {
            // Roll back the optimistic delete if the server rejected it.
            if (removed) {
                set((state) => {
                    const next = [...state.recentExecutions, removed].sort(
                        (a, b) => new Date(b.started_at) - new Date(a.started_at),
                    );
                    return {
                        recentExecutions: next,
                        unseenCount: next.filter((r) => !r.seen).length,
                    };
                });
            }
            return false;
        }
    },

    clearAllExecutions: async () => {
        try {
            await apiFetch('/trigger-executions', { method: 'DELETE' });
            set({ recentExecutions: [], unseenCount: 0 });
            return true;
        } catch {
            return false;
        }
    },

    loadHistory: async (triggerId) => {
        if (!triggerId) return [];
        try {
            const params = new URLSearchParams({ trigger_id: triggerId, limit: '20' });
            const rows = await apiFetch(`/trigger-executions?${params}`);
            set((state) => ({
                historyByTrigger: { ...state.historyByTrigger, [triggerId]: rows || [] },
            }));
            return rows || [];
        } catch {
            return [];
        }
    },

    loadExecution: async (executionId) => {
        try {
            return await apiFetch(`/trigger-executions/${executionId}`);
        } catch {
            return null;
        }
    },

    clearError: () => set({ error: null }),
}));

export default useTriggersStore;
