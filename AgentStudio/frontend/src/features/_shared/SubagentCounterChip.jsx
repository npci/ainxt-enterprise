// SPDX-License-Identifier: MIT
import { memo, useState, useCallback } from 'react';

const CHEVRON_RIGHT = (
    <svg width="10" height="10" viewBox="0 0 16 16" aria-hidden="true">
        <path d="M5.5 3.5 L10.5 8 L5.5 12.5" fill="none"
              stroke="currentColor" strokeWidth="1.6"
              strokeLinecap="round" strokeLinejoin="round" />
    </svg>
);

const STATUS_LABEL = {
    running:  'running',
    complete: 'done',
    failed:   'failed',
};

/**
 * Live "N sub-agents working" counter chip + accordion.
 *
 * Visual pattern (minimal display — names + status + duration only):
 *   ▸ 3 sub-agents working                           (collapsed header — default)
 *   ▼ 3 sub-agents working
 *       extractor  · running
 *       classifier · 1.2s · done
 *       ranker     · running
 *
 * State model:
 *   - The HEADER is collapsed by default; clicking it reveals the list.
 *   - Per-row expansion has been removed: tool lists, task previews,
 *     error detail payloads, and result previews are intentionally
 *     hidden so a noisy planner failure (e.g. orchestrator schema
 *     validation error) does not splatter the chat panel. Full
 *     payloads remain on the SSE stream for API consumers.
 *
 * Props:
 *   count     — number of CURRENTLY in-flight sub-agents (drives header label)
 *   workers   — legacy: array of {alias} for the title tooltip when no `subagents`
 *   subagents — full list from selectAllSubagents(): {callId, alias, status,
 *               taskPreview, durationS, error, preview}. When omitted, the
 *               component falls back to the old chip-only behaviour so
 *               existing call sites don't break.
 *
 * Returns null when count === 0 AND there are no completed sub-agents
 * to show — the parent can render unconditionally without flicker.
 */
const SubagentCounterChip = memo(function SubagentCounterChip({ count, workers, subagents }) {
    const hasList    = Array.isArray(subagents) && subagents.length > 0;
    const totalCount = hasList ? subagents.length : (count || 0);
    const liveCount  = hasList
        ? subagents.filter((s) => s && s.status === 'running').length
        : (count || 0);

    // Open/closed state for the whole accordion (the chip header acts as
    // a button). Per-row expansion was removed — minimal-display mode
    // only shows alias + status + duration per row, so there is nothing
    // to expand into.
    const [listOpen, setListOpen] = useState(false);

    const toggleList = useCallback(() => {
        setListOpen((v) => !v);
    }, []);

    if (!totalCount || totalCount <= 0) return null;

    const label = liveCount > 0
        ? `${liveCount} sub-agent${liveCount === 1 ? '' : 's'} working`
        : `${totalCount} sub-agent${totalCount === 1 ? '' : 's'} done`;

    const headerTitle = hasList
        ? subagents.map((s) => s?.alias).filter(Boolean).join(', ')
        : (Array.isArray(workers)
            ? workers.map((w) => w?.alias).filter(Boolean).join(', ')
            : undefined) || undefined;

    return (
        <span className="thinking-subagent-shell" data-testid="subagent-shell">
            <button
                type="button"
                className={`thinking-subagent-counter ${listOpen ? 'is-open' : ''} ${hasList ? 'is-clickable' : ''}`}
                title={headerTitle}
                aria-live="polite"
                aria-expanded={hasList ? listOpen : undefined}
                onClick={hasList ? toggleList : undefined}
                disabled={!hasList}
            >
                {liveCount > 0
                    ? <span className="thinking-subagent-counter-dot" aria-hidden="true" />
                    : <span className="thinking-subagent-counter-dot is-idle" aria-hidden="true" />
                }
                {label}
                {hasList && (
                    <span className={`thinking-subagent-chevron ${listOpen ? 'is-open' : ''}`}
                          aria-hidden="true">
                        {CHEVRON_RIGHT}
                    </span>
                )}
            </button>

            {hasList && listOpen && (
                <ul className="thinking-subagent-list" data-testid="subagent-list">
                    {subagents.map((s) => {
                        if (!s) return null;
                        const stateLabel = STATUS_LABEL[s.status] || s.status;
                        const durationText = (s.status !== 'running' && typeof s.durationS === 'number')
                            ? ` · ${s.durationS}s`
                            : '';
                        // Minimal-display mode: show ONLY the sub-agent
                        // name + status + duration. No expanded body —
                        // task previews, tool lists, raw error / detail
                        // payloads, and result previews are all hidden
                        // so a noisy planner failure (e.g. orchestrator
                        // schema validation error) does not splatter
                        // across the chat panel. Operators get a clean
                        // "N sub-agents ran, here's how long each took"
                        // summary; full payloads remain on the SSE
                        // stream for the API consumer.
                        return (
                            <li key={s.callId}
                                className={`thinking-subagent-row thinking-subagent-row--${s.status}`}
                                data-testid="subagent-row"
                                data-call-id={s.callId}>
                                <div className="thinking-subagent-row-head">
                                    <span className={`thinking-subagent-row-dot thinking-subagent-row-dot--${s.status}`}
                                          aria-hidden="true" />
                                    <span className="thinking-subagent-row-alias">{s.alias}</span>
                                    <span className="thinking-subagent-row-meta">
                                        · {stateLabel}{durationText}
                                    </span>
                                </div>
                            </li>
                        );
                    })}
                </ul>
            )}
        </span>
    );
});

export default SubagentCounterChip;
