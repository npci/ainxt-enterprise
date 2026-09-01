// SPDX-License-Identifier: Apache-2.0
import { create } from 'zustand';
import { apiFetch } from '../config/api';
import { validateEntityName } from '../utils/validateName';

const useDashboardStore = create((set, get) => ({
    workflows: [],
    templates: [],
    isLoading: false,
    error: null,
    activeTab: 'drafts',

    setActiveTab: (tab) => set({ activeTab: tab }),

    loadWorkflows: async () => {
        set({ isLoading: true, error: null });
        try {
            const data = await apiFetch('/workflows');
            set({ workflows: data, isLoading: false });
        } catch (error) {
            set({ error: error.message, isLoading: false });
        }
    },

    loadTemplates: async () => {
        try {
            const data = await apiFetch('/templates');
            set({ templates: data });
        } catch {
            // templates failing is non-critical
        }
    },

    createWorkflow: async (data = {}) => {
        const requestedName = data.name || 'New workflow';
        // Pre-validate the name client-side so the user gets immediate
        // feedback (and we avoid a wasted round-trip) when it's clearly bad.
        // We pass existing workflow names so duplicate-name errors fire here
        // rather than after the API call.
        const existingNames = (get().workflows || []).map((w) => w.name);
        const validationError = validateEntityName(requestedName, 'workflow', { existingNames });
        if (validationError) {
            set({ error: validationError });
            return null;
        }
        try {
            const wf = await apiFetch('/workflows', {
                method: 'POST',
                body: JSON.stringify({
                    name:        requestedName,
                    description: data.description || '',
                    graphData:   data.graphData   || { nodes: [], edges: [] },
                }),
            });
            set((state) => ({ workflows: [wf, ...state.workflows], error: null }));
            return wf;
        } catch (error) {
            set({ error: error.message });
            return null;
        }
    },

    updateWorkflow: async (id, data) => {
        // If the update changes the name, run the same validation we use at
        // create time. ``currentName`` excludes the row itself from the
        // uniqueness check so a no-op rename doesn't trip the duplicate rule.
        if (data && Object.prototype.hasOwnProperty.call(data, 'name')) {
            const current = (get().workflows || []).find((w) => w.id === id);
            const existingNames = (get().workflows || []).map((w) => w.name);
            const validationError = validateEntityName(data.name, 'workflow', {
                existingNames,
                currentName: current ? current.name : undefined,
            });
            if (validationError) {
                set({ error: validationError });
                return null;
            }
        }
        try {
            const wf = await apiFetch(`/workflows/${id}`, {
                method: 'PUT',
                body: JSON.stringify(data),
            });
            set((state) => ({
                workflows: state.workflows.map((w) => (w.id === id ? wf : w)),
                error: null,
            }));
            return wf;
        } catch (error) {
            set({ error: error.message });
            throw error;
        }
    },

    deleteWorkflow: async (id) => {
        try {
            await apiFetch(`/workflows/${id}`, { method: 'DELETE' });
            set((state) => ({
                workflows: state.workflows.filter((w) => w.id !== id),
            }));
            return true;
        } catch (error) {
            set({ error: error.message });
            return false;
        }
    },

    duplicateWorkflow: async (id) => {
        try {
            const wf = await apiFetch(`/workflows/${id}/duplicate`, { method: 'POST' });
            set((state) => ({ workflows: [wf, ...state.workflows] }));
            return wf;
        } catch (error) {
            set({ error: error.message });
            return null;
        }
    },

    useTemplate: async (id) => {
        try {
            const wf = await apiFetch(`/templates/${id}/use`, { method: 'POST' });
            set((state) => ({
                workflows: state.workflows.some((w) => w.id === wf.id)
                    ? state.workflows.map((w) => (w.id === wf.id ? wf : w))
                    : [wf, ...state.workflows],
            }));
            return wf;
        } catch (error) {
            set({ error: error.message });
            return null;
        }
    },

    clearError: () => set({ error: null }),
}));

export default useDashboardStore;
