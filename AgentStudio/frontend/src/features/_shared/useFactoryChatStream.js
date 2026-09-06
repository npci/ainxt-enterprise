// SPDX-License-Identifier: MIT
import { useCallback, useRef, useState } from 'react';
import { API_BASE, buildAuthHeaders } from '../../config/api';

/**
 * useFactoryChatStream — shared SSE chat engine for the three "Create with AI"
 * factory chats (Workflow / Agent / Skill).
 *
 * All three previously duplicated the same fetch → getReader() → parse loop plus
 * the accumulating "steps" block state. This hook owns that common
 * logic once, fixing shared bugs (notably the `Math.random()` message-key churn,
 * now a stable incrementing counter) and keeping a single event contract:
 *   - { type: 'thinking', text }      → appends a step line
 *   - { type: 'message', text, stage, suggestions?, data? } → finalizes steps,
 *                                        adds an assistant bubble, updates stage
 *                                        + suggestions, then calls onMessage(ev)
 *   - { type: 'error', message }      → removes steps, adds an error bubble
 *   - { type: 'done', session_id }    → stores session id, clears loading
 *
 * Feature-specific side effects (e.g. workflow sets workflowData, agent/skill
 * set assembled, both handle existing_matches) are handled by the caller via the
 * `onMessage` callback so this hook stays generic.
 *
 * @param {object}   opts
 * @param {string}   opts.endpoint   API path, e.g. '/workflow-factory/chat'
 * @param {function} [opts.onMessage] (ev) => void — called on each 'message' event
 * @param {function} [opts.onStageChange] (stage) => void
 * @param {function} [opts.onReset]  () => void — called at the start of each send
 */

// Map internal Plan Card answer IDs to short, human-readable labels so the
// chat bubble reads naturally (e.g. "Trigger: API call · Failure: Retry
// automatically"). Unknown IDs fall back to a Title-cased version of the id.
const _PLAN_CARD_LABELS = {
    audience: 'Audience', refusal_scope: 'Never do', tone: 'Tone',
    escalation: 'On failure', detail_level: 'Detail',
    repos: 'Repos', branches: 'Branches', languages: 'Languages',
    code_tasks: 'Code tasks', autonomous_approve: 'Autonomy',
    trigger_type: 'Trigger', failure_policy: 'On step failure',
    approval_gate: 'Approval', step_count: 'Steps',
    share_context: 'Context sharing', external_systems: 'Connects to',
    output_format: 'Output', avoid_when: "Don't use when",
    include_examples: 'Examples',
};

function formatPlanCardAnswers(answers) {
    if (!answers || typeof answers !== 'object') return 'Generate with these settings';
    const parts = [];
    for (const [id, val] of Object.entries(answers)) {
        if (id.startsWith('_')) continue; // internal flags (e.g. _svc_warning_ack)
        let shown = Array.isArray(val) ? val.filter(Boolean).join(', ') : val;
        if (shown === undefined || shown === null || shown === '') continue;
        const label = _PLAN_CARD_LABELS[id] || id.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
        parts.push(`${label}: ${shown}`);
    }
    if (parts.length === 0) return 'Generate with these settings';
    return 'Generate with these settings —\n' + parts.map((p) => `• ${p}`).join('\n');
}

export function useFactoryChatStream({ endpoint, onMessage, onStageChange, onReset, buildExtraBody }) {
    const [messages, setMessages] = useState([]);
    const [suggestions, setSuggestions] = useState([]);
    const [stage, setStage] = useState('clarifying');
    const [isLoading, setIsLoading] = useState(false);
    const [sessionId, setSessionId] = useState(null);

    // Monotonic id source — stable, collision-free keys without Math.random()
    // (which produced a new key every render → remounts + scroll jumps).
    const idRef = useRef(0);
    const nextId = useCallback((prefix) => {
        idRef.current += 1;
        return `${prefix}-${idRef.current}`;
    }, []);

    const stepsIdRef = useRef(null);

    // --- Steps block helpers (accumulating progress) ---
    const addStepsBlock = useCallback((id) => {
        setMessages((prev) => [...prev, { id, type: 'steps', steps: [], done: false }]);
    }, []);

    const appendStep = useCallback((blockId, text) => {
        setMessages((prev) => prev.map((m) => {
            if (m.id !== blockId || m.type !== 'steps') return m;
            const steps = [...m.steps];
            if (steps.length > 0) {
                steps[steps.length - 1] = { ...steps[steps.length - 1], status: 'done' };
            }
            steps.push({ text, status: 'active' });
            return { ...m, steps };
        }));
    }, []);

    const finalizeSteps = useCallback((blockId) => {
        setMessages((prev) => prev.map((m) => {
            if (m.id !== blockId || m.type !== 'steps') return m;
            return { ...m, steps: m.steps.map((s) => ({ ...s, status: 'done' })), done: true };
        }));
    }, []);

    const removeSteps = useCallback((blockId) => {
        setMessages((prev) => prev.filter((m) => m.id !== blockId));
    }, []);

    const sendMessage = useCallback(async (text) => {
        if (!text || isLoading) return;
        setIsLoading(true);
        onReset?.();
        setSuggestions([]);
        // Plan Card answers are sent as an internal ``__plan_card__:{json}``
        // control string. Never render the raw protocol string as a chat bubble
        // — instead summarise the user's selections so they can see (and
        // remember) exactly what they chose. Hide the silent "Continue anyway"
        // resend so the summary isn't shown twice.
        const isPlanCard = typeof text === 'string' && text.startsWith('__plan_card__:');
        let bubbleContent = text;
        let hideBubble = false;
        if (isPlanCard) {
            bubbleContent = 'Generate with these settings';
            try {
                const parsed = JSON.parse(text.slice('__plan_card__:'.length));
                if (parsed && parsed._svc_warning_ack) {
                    hideBubble = true; // "Continue anyway" resend — already summarised
                } else {
                    bubbleContent = formatPlanCardAnswers(parsed);
                }
            } catch { /* keep default label */ }
        }
        if (!hideBubble) {
            setMessages((prev) => [...prev, { id: nextId('user'), role: 'user', content: bubbleContent }]);
        }

        const stepsId = nextId('steps');
        stepsIdRef.current = stepsId;
        addStepsBlock(stepsId);

        try {
            const extraBody = (typeof buildExtraBody === 'function') ? (buildExtraBody() || {}) : {};
            const res = await fetch(`${API_BASE}${endpoint}`, {
                method: 'POST',
                headers: buildAuthHeaders(),
                body: JSON.stringify({ session_id: sessionId, message: text, ...extraBody }),
            });

            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() ?? '';

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    let ev;
                    try { ev = JSON.parse(line.slice(6)); } catch { continue; }

                    if (ev.type === 'thinking') {
                        appendStep(stepsId, ev.text);
                    } else if (ev.type === 'message') {
                        // If no progress steps were streamed (e.g. a greeting
                        // short-circuit), drop the empty steps block entirely so
                        // it doesn't render as a blank "thinking" bar above the
                        // reply. Otherwise finalize it as normal.
                        setMessages((prev) => {
                            const block = prev.find((m) => m.id === stepsId && m.type === 'steps');
                            const isEmpty = block && (!block.steps || block.steps.length === 0);
                            const base = isEmpty
                                ? prev.filter((m) => m.id !== stepsId)
                                : prev.map((m) => (
                                    m.id === stepsId && m.type === 'steps'
                                        ? { ...m, steps: m.steps.map((s) => ({ ...s, status: 'done' })), done: true }
                                        : m
                                ));
                            return [
                                ...base,
                                { id: nextId('ai'), role: 'assistant', content: ev.text, stage: ev.stage },
                            ];
                        });
                        if (ev.stage) {
                            setStage(ev.stage);
                            onStageChange?.(ev.stage);
                        }
                        if (ev.suggestions?.length > 0) {
                            setSuggestions(ev.suggestions.map((s) =>
                                typeof s === 'string' ? { icon: '💡', label: s } : s));
                        } else {
                            setSuggestions([]);
                        }
                        onMessage?.(ev);
                    } else if (ev.type === 'error') {
                        removeSteps(stepsId);
                        setMessages((prev) => [
                            ...prev,
                            { id: nextId('err'), role: 'assistant', content: ev.message, isError: true },
                        ]);
                        setIsLoading(false);
                    } else if (ev.type === 'done') {
                        if (ev.session_id) setSessionId(ev.session_id);
                        setIsLoading(false);
                    }
                }
            }
        } catch (err) {
            removeSteps(stepsIdRef.current);
            setMessages((prev) => [
                ...prev.filter((m) => m.type !== 'steps'),
                { id: nextId('err'), role: 'assistant', content: `Something went wrong: ${err.message}`, isError: true },
            ]);
        } finally {
            setIsLoading(false);
        }
    }, [endpoint, sessionId, isLoading, nextId, addStepsBlock, appendStep,
        finalizeSteps, removeSteps, onMessage, onStageChange, onReset, buildExtraBody]);

    return {
        // state
        messages, suggestions, stage, isLoading, sessionId,
        // setters the feature may still need
        setMessages, setSuggestions, setStage, setSessionId,
        // actions
        sendMessage, nextId,
    };
}

export default useFactoryChatStream;
