// SPDX-License-Identifier: MIT
// Optional template editor store — paired with `app/api/template_admin.py`
// on the backend. To remove this feature, delete:
//   - this file
//   - src/features/templates/TemplateEditModal.jsx
//   - src/features/templates/TemplateCardMenu.jsx
//   - the editingTemplateId branch in App.jsx
//   - the menu render block in WorkflowsDashboard.jsx
//
// The dashboard store and the canvas editor have zero references to this
// module, so deletion is mechanical.
import { create } from 'zustand';
import { apiFetch } from '../config/api';

const useTemplateAdminStore = create((set, get) => ({
    // null = not yet probed, true/false = backend said so.
    isEditable: null,
    // Whichever template the edit-metadata modal is open for, or null.
    editingTemplate: null,
    error: null,

    loadStatus: async () => {
        try {
            const data = await apiFetch('/template-admin/status');
            set({ isEditable: !!(data && data.editable) });
        } catch {
            // The endpoint returns 404 when the feature flag is off. Treat
            // any failure as "not editable" so the UI hides the controls.
            set({ isEditable: false });
        }
    },

    openEditModal: (template) => set({ editingTemplate: template, error: null }),
    closeEditModal: () => set({ editingTemplate: null, error: null }),

    updateTemplate: async (id, patch) => {
        try {
            const updated = await apiFetch(`/template-admin/${encodeURIComponent(id)}`, {
                method: 'PUT',
                body: JSON.stringify(patch),
            });
            set({ error: null });
            return updated;
        } catch (e) {
            set({ error: e.message || 'Update failed' });
            return null;
        }
    },

    deleteTemplate: async (id) => {
        try {
            await apiFetch(`/template-admin/${encodeURIComponent(id)}`, { method: 'DELETE' });
            set({ error: null });
            return true;
        } catch (e) {
            set({ error: e.message || 'Delete failed' });
            return false;
        }
    },

    resetTemplate: async (id) => {
        try {
            const restored = await apiFetch(
                `/template-admin/${encodeURIComponent(id)}/reset`,
                { method: 'POST' },
            );
            set({ error: null });
            return restored;
        } catch (e) {
            set({ error: e.message || 'Reset failed' });
            return null;
        }
    },

    saveToSeed: async (id) => {
        try {
            const saved = await apiFetch(
                `/template-admin/${encodeURIComponent(id)}/save-to-seed`,
                { method: 'POST' },
            );
            set({ error: null });
            return saved;
        } catch (e) {
            set({ error: e.message || 'Save to seed failed' });
            return null;
        }
    },

    createTemplate: async (payload) => {
        try {
            const created = await apiFetch('/template-admin', {
                method: 'POST',
                body: JSON.stringify(payload),
            });
            set({ error: null });
            return created;
        } catch (e) {
            set({ error: e.message || 'Create failed' });
            return null;
        }
    },

    clearError: () => set({ error: null }),
}));

export default useTemplateAdminStore;
