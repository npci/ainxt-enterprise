// SPDX-License-Identifier: Apache-2.0
import { create } from 'zustand';
import { apiFetch } from '../config/api';
import { validateEntityName } from '../utils/validateName';
import { KB_MODE_NONE } from '../components/common/KnowledgeSection';

// Dedupe + short-TTL cache for GET /agents. Multiple components mount in the
// same tick (dashboard, editor, config panel, subflow picker, …) and each
// calls loadAgents() — without this we fire one identical request per caller.
const AGENTS_TTL_MS = 5000;
let _agentsInFlight = null;     // shared promise while a fetch is pending
let _agentsLastFetch = 0;       // epoch ms of last successful fetch
const invalidateAgentsCache = () => { _agentsLastFetch = 0; };

const useAgentsStore = create((set, get) => ({
    agents: [],
    agentTemplates: [],
    isLoading: false,
    error: null,
    activeTab: 'my-agents',

    setActiveTab: (tab) => set({ activeTab: tab }),

    loadAgents: async ({ force = false } = {}) => {
        // Reuse an in-flight request so a burst of mounts collapses to one GET.
        if (_agentsInFlight) return _agentsInFlight;

        // Skip if we fetched recently and the caller didn't ask for fresh data.
        // Use `_agentsLastFetch > 0` instead of `get().agents.length` so an
        // empty agents list still counts as a successful fetch — otherwise
        // users with zero agents bypass the cache on every call, which (when
        // combined with an upstream effect that re-runs on state mutations)
        // produces a runaway GET /agents loop.
        if (!force && _agentsLastFetch > 0 && Date.now() - _agentsLastFetch < AGENTS_TTL_MS) {
            return get().agents;
        }

        set({ isLoading: true, error: null });
        _agentsInFlight = (async () => {
            try {
                const data = await apiFetch('/agents');
                _agentsLastFetch = Date.now();
                set({ agents: data, isLoading: false });
                return data;
            } catch (error) {
                set({ error: error.message, isLoading: false });
                throw error;
            } finally {
                _agentsInFlight = null;
            }
        })();
        return _agentsInFlight;
    },

    loadAgentTemplates: async () => {
        try {
            const data = await apiFetch('/agent-templates');
            set({ agentTemplates: data });
        } catch {
            // agent templates failing is non-critical
        }
    },

    useAgentTemplate: async (templateId) => {
        try {
            const agent = await apiFetch(`/agent-templates/${templateId}/use`, { method: 'POST' });
            invalidateAgentsCache();
            set((state) => ({
                agents: state.agents.some((a) => a.id === agent?.id)
                    ? state.agents.map((a) => (a.id === agent.id ? agent : a))
                    : [agent, ...state.agents],
            }));
            return agent;
        } catch (error) {
            set({ error: error.message });
            return null;
        }
    },

    createAgent: async (data = {}) => {
        const requestedName = data.name || 'New Agent';
        // Mirror backend validation so the user sees errors immediately, and
        // catch name collisions before issuing the POST.
        const existingNames = (get().agents || []).map((a) => a.name);
        const validationError = validateEntityName(requestedName, 'agent', { existingNames });
        if (validationError) {
            set({ error: validationError });
            throw new Error(validationError);
        }
        try {
            const agent = await apiFetch('/agents', {
                method: 'POST',
                body: JSON.stringify({
                    name:          requestedName,
                    description:   data.description   || '',
                    instructions:  data.instructions  || '',
                    provider:      data.provider     || 'google',
                    model_name:    data.model_name    || '',
                    api_key:       data.api_key       || '',
                    temperature:   data.temperature   ?? 0.7,
                    max_tokens:    data.max_tokens    ?? 2048,
                    top_p:         data.top_p         ?? 1.0,
                    base_url:      data.base_url      || '',
                    tools:         data.tools         || [],
                    skills:        data.skills        || [],
                    guardrails:    data.guardrails    || {},
                    memory_config: data.memory_config || {},
                    // Previously omitted from the create payload, so any KB
                    // the user picked before the first save was silently
                    // dropped (the backend's column default masked it).
                    knowledge:     data.knowledge     || { mode: KB_MODE_NONE },
                    // Per-agent swarm/subagents delegation. Default OFF so a
                    // brand-new agent never gets spawn_swarm unless opted in.
                    use_subagents: data.use_subagents ?? false,
                }),
            });
            invalidateAgentsCache();
            set((state) => ({
                agents: state.agents.some((a) => a.id === agent?.id)
                    ? state.agents.map((a) => (a.id === agent.id ? agent : a))
                    : [agent, ...state.agents],
                error: null,
            }));
            return agent;
        } catch (error) {
            set({ error: error.message });
            throw error;
        }
    },

    updateAgent: async (id, data) => {
        if (data && Object.prototype.hasOwnProperty.call(data, 'name')) {
            const current = (get().agents || []).find((a) => a.id === id);
            const existingNames = (get().agents || []).map((a) => a.name);
            const validationError = validateEntityName(data.name, 'agent', {
                existingNames,
                currentName: current ? current.name : undefined,
            });
            if (validationError) {
                set({ error: validationError });
                throw new Error(validationError);
            }
        }
        try {
            const agent = await apiFetch(`/agents/${id}`, {
                method: 'PUT',
                body: JSON.stringify(data),
            });
            invalidateAgentsCache();
            set((state) => ({
                agents: state.agents.map((a) => (a.id === id ? agent : a)),
                error: null,
            }));
            return agent;
        } catch (error) {
            set({ error: error.message });
            throw error;
        }
    },

    deleteAgent: async (id) => {
        try {
            await apiFetch(`/agents/${id}`, { method: 'DELETE' });
            invalidateAgentsCache();
            set((state) => ({
                agents: state.agents.filter((a) => a.id !== id),
            }));
            return true;
        } catch (error) {
            set({ error: error.message });
            return false;
        }
    },

    duplicateAgent: async (id) => {
        try {
            const agent = await apiFetch(`/agents/${id}/duplicate`, { method: 'POST' });
            invalidateAgentsCache();
            set((state) => ({
                agents: state.agents.some((a) => a.id === agent?.id)
                    ? state.agents.map((a) => (a.id === agent.id ? agent : a))
                    : [agent, ...state.agents],
            }));
            return agent;
        } catch (error) {
            set({ error: error.message });
            return null;
        }
    },

    clearError: () => set({ error: null }),
}));

export default useAgentsStore;
