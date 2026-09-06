// SPDX-License-Identifier: MIT
/**
 * Pure helpers that derive sub-agent state from the workflowStore
 * `executionLogs` array. Extracted so we can unit-test them without
 * mounting ChatPanel.jsx (which pulls in the entire workflow editor).
 */

/**
 * Compute the live "in-flight sub-agents" list from a chronological
 * stream of executionLogs.
 *
 * Walks the log entries in order, adding a worker on each
 * `subagent_start` event and removing it on the matching
 * `subagent_complete` (matched by call_id). Returns the still-running
 * workers in start order so any UI tooltip lists them deterministically.
 *
 * Pure: same inputs ⇒ same output. Safe to memoise on the log array
 * reference.
 *
 * @param {Array<Object>} executionLogs
 * @returns {Array<{callId: string, alias: string, agentId: string, taskPreview: string}>}
 */
export function selectActiveSubagents(executionLogs) {
    if (!Array.isArray(executionLogs) || executionLogs.length === 0) return [];
    const live = new Map();
    for (const log of executionLogs) {
        if (!log) continue;
        if (log.type === 'subagent_start') {
            live.set(log.callId, {
                callId:      log.callId,
                alias:       log.alias,
                agentId:     log.agentId,
                taskPreview: log.taskPreview,
            });
        } else if (log.type === 'subagent_complete') {
            live.delete(log.callId);
        }
    }
    return Array.from(live.values());
}


/**
 * Compute the ALL-sub-agents list (running + complete + failed) from a
 * chronological stream of executionLogs.
 *
 * Powers the accordion under the counter chip: the user wants to see
 * every sub-agent that participated in this turn, even after it
 * finished, so they can click in and inspect what it did. Unlike
 * `selectActiveSubagents`, entries are NOT removed on completion —
 * the matching start row is mutated in place with the completion
 * fields. Order is start-order so the UI list is deterministic.
 *
 * Status values:
 *   'running'  — subagent_start seen, no matching subagent_complete yet
 *   'complete' — subagent_complete seen with ok === true
 *   'failed'   — subagent_complete seen with ok === false
 *
 * @param {Array<Object>} executionLogs
 * @returns {Array<{
 *   callId: string,
 *   alias: string,
 *   agentId: string,
 *   parentAgentId?: string,
 *   taskPreview: string,
 *   status: 'running'|'complete'|'failed',
 *   durationS?: number,
 *   error?: string|null,
 *   preview?: string,
 *   files?: Array,
 * }>}
 */
export function selectAllSubagents(executionLogs) {
    if (!Array.isArray(executionLogs) || executionLogs.length === 0) return [];
    const byId   = new Map();   // callId -> row
    const order  = [];          // insertion order (start order)
    for (const log of executionLogs) {
        if (!log) continue;
        // ``swarm_plan`` fires BEFORE any ``subagent_start`` — surface the
        // planned worker names immediately so the chat panel shows
        // "Planning N sub-agents" instead of an empty spawn_swarm chip
        // during the orchestrator LLM call (typically 2-10s).
        //
        // Synthetic callId: ``planned::<role>``. When the real
        // ``subagent_start`` arrives we replace the placeholder so the row
        // doesn't duplicate (matched by alias).
        if (log.type === 'swarm_plan') {
            const roles = Array.isArray(log.roleIds) ? log.roleIds : [];
            for (const role of roles) {
                const placeholderId = `planned::${role}`;
                if (!byId.has(placeholderId)) {
                    byId.set(placeholderId, {
                        callId:        placeholderId,
                        alias:         role,
                        agentId:       '',
                        parentAgentId: '',
                        taskPreview:   '',
                        status:        'planning',
                    });
                    order.push(placeholderId);
                }
            }
            continue;
        }
        // ``swarm_error`` lets the panel show why the swarm couldn't
        // run (plan_validation_failed, orchestrator_failure, etc.)
        // instead of silently waiting for the parent's paraphrase.
        if (log.type === 'swarm_error') {
            const errId = `swarm_error::${log.code || 'unknown'}::${log.runId || ''}`;
            if (!byId.has(errId)) {
                byId.set(errId, {
                    callId:        errId,
                    alias:         log.code || 'swarm_error',
                    agentId:       '',
                    parentAgentId: '',
                    taskPreview:   '',
                    status:        'failed',
                    error:         log.code || 'swarm_error',
                    preview:       log.detail || '',
                });
                order.push(errId);
            }
            continue;
        }
        if (!log.callId) continue;
        if (log.type === 'subagent_start') {
            // Upgrade a planning placeholder (matched by alias) to the real
            // row in place — keeps the timeline order stable and avoids a
            // duplicate "planned" + "running" pair for the same worker.
            const placeholderId = `planned::${log.alias}`;
            const placeholder = byId.get(placeholderId);
            if (placeholder && placeholder.status === 'planning') {
                // Re-key the entry under the real callId so the subsequent
                // ``subagent_complete`` (matched by callId) finds it.
                byId.delete(placeholderId);
                const idx = order.indexOf(placeholderId);
                if (idx >= 0) order[idx] = log.callId;
                byId.set(log.callId, {
                    callId:        log.callId,
                    alias:         log.alias,
                    agentId:       log.agentId,
                    parentAgentId: log.parentAgentId,
                    taskPreview:   log.taskPreview || '',
                    tools:         Array.isArray(log.tools)  ? log.tools  : [],
                    skills:        Array.isArray(log.skills) ? log.skills : [],
                    status:        'running',
                });
                continue;
            }
            if (!byId.has(log.callId)) {
                byId.set(log.callId, {
                    callId:        log.callId,
                    alias:         log.alias,
                    agentId:       log.agentId,
                    parentAgentId: log.parentAgentId,
                    taskPreview:   log.taskPreview || '',
                    tools:         Array.isArray(log.tools)  ? log.tools  : [],
                    skills:        Array.isArray(log.skills) ? log.skills : [],
                    status:        'running',
                });
                order.push(log.callId);
            }
        } else if (log.type === 'subagent_complete') {
            const row = byId.get(log.callId);
            if (row) {
                row.status    = log.ok === false ? 'failed' : 'complete';
                row.durationS = typeof log.durationS === 'number' ? log.durationS : undefined;
                row.error     = log.error || null;
                row.preview   = log.preview || '';
                row.files     = log.files || [];
            } else {
                // Defensive: a completion arriving before its start.
                // Surface it so the user can still see it instead of
                // silently dropping the event.
                byId.set(log.callId, {
                    callId:      log.callId,
                    alias:       log.alias || '(unknown)',
                    agentId:     log.agentId,
                    parentAgentId: log.parentAgentId,
                    taskPreview: '',
                    status:      log.ok === false ? 'failed' : 'complete',
                    durationS:   typeof log.durationS === 'number' ? log.durationS : undefined,
                    error:       log.error || null,
                    preview:     log.preview || '',
                    files:       log.files || [],
                });
                order.push(log.callId);
            }
        }
    }
    return order.map((id) => byId.get(id));
}
