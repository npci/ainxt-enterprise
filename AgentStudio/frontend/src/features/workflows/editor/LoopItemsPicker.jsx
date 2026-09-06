// SPDX-License-Identifier: MIT
import { useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '../../../config/api';
import { findListsInOutput, parseUpstreamOutput } from './helpers/loopPicker';

const DEFAULT_PATH = 'input.items';

/**
 * Turn a dotted path into a human label so the user never sees the raw
 * `input.x.y` syntax. `input.results.docs` becomes "Docs", `input.items`
 * becomes "Items", and the root array becomes "Whole output".
 */
function humaniseLabel(path) {
    if (!path || path === 'input') return 'Whole output';
    const last = String(path).split('.').filter(Boolean).pop() || 'List';
    return last
        .replace(/[_-]+/g, ' ')
        .replace(/([a-z])([A-Z])/g, '$1 $2')
        .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Connection-aware list picker for the Loop node's "for each" mode.
 *
 * Goals:
 *   1. The user never has to read or type a dotted path.
 *   2. When the upstream node has produced exactly one list, it is selected
 *      silently. The user does nothing.
 *   3. When the upstream node produced several lists, the user picks one
 *      from a plain-language dropdown ("Docs (5 items)" — not
 *      "input.results.docs").
 *   4. When the workflow has not been run yet, the user gets a single
 *      actionable hint: run the workflow once and come back. The loop keeps
 *      a safe default in the meantime so they can still save the canvas.
 *
 * The dotted-path value is still maintained in the store for the backend
 * engine, but it is never surfaced as an editable string here.
 */
function LoopItemsPicker({
    value,
    onChange,
    upstreamNodeId,
    upstreamNodeName,
    threadId,
}) {
    const [output, setOutput] = useState(null);
    const [status, setStatus] = useState('idle'); // idle | loading | loaded | error
    const [error, setError] = useState('');

    useEffect(() => {
        // Reset whenever the wiring or thread context changes so a stale
        // detection from a previous upstream does not bleed through.
        setOutput(null);
        setError('');
        if (!upstreamNodeId || !threadId) {
            setStatus('idle');
            return;
        }
        let cancelled = false;
        setStatus('loading');
        apiFetch(`/node-last-output/${encodeURIComponent(threadId)}/${encodeURIComponent(upstreamNodeId)}`)
            .then((data) => {
                if (cancelled) return;
                setOutput(data?.output ?? null);
                setStatus('loaded');
            })
            .catch((e) => {
                if (cancelled) return;
                setError(e?.message || 'Failed to read upstream output');
                setStatus('error');
            });
        return () => { cancelled = true; };
    }, [upstreamNodeId, threadId]);

    const detectedLists = useMemo(() => {
        if (output == null) return [];
        const parsed = parseUpstreamOutput(output);
        return parsed == null ? [] : findListsInOutput(parsed);
    }, [output]);

    // When exactly one list is detected and the user has not already pinned
    // a different path, lock the items expression to it silently. The picker
    // tracks the last auto-assigned path so a subsequent manual override is
    // never clobbered by a later auto-detect with the same single result.
    const autoAssignedRef = useRef(null);
    useEffect(() => {
        if (detectedLists.length !== 1) return;
        const only = detectedLists[0].path;
        if (value === only) return;
        const userHadDefault = !value || value === DEFAULT_PATH;
        const userPickedAuto = autoAssignedRef.current && value === autoAssignedRef.current;
        if (!userHadDefault && !userPickedAuto) return;
        autoAssignedRef.current = only;
        onChange(only);
    }, [detectedLists, value, onChange]);

    const choiceOptions = detectedLists.map((entry) => ({
        path: entry.path,
        label: humaniseLabel(entry.path),
        count: entry.length,
    }));

    // ---- Render branches ---------------------------------------------------

    if (!upstreamNodeId) {
        return (
            <div className="loop-picker-card loop-picker-card--muted">
                Connect a node into the top of this loop. The list it produces will
                show up here automatically.
            </div>
        );
    }

    const friendlyUpstream = upstreamNodeName || 'the upstream node';

    if (!threadId) {
        return (
            <div className="loop-picker-card loop-picker-card--muted">
                Run the workflow once from the chat panel so this loop can see what
                <strong> {friendlyUpstream}</strong> produces. Until then it will iterate
                the default list from that node.
            </div>
        );
    }

    if (status === 'loading') {
        return (
            <div className="loop-picker-card loop-picker-card--muted">
                Reading <strong>{friendlyUpstream}</strong>&apos;s last output…
            </div>
        );
    }

    if (status === 'error') {
        return (
            <div className="loop-picker-card loop-picker-card--warn">{error}</div>
        );
    }

    if (detectedLists.length === 0) {
        return (
            <div className="loop-picker-card loop-picker-card--warn">
                <strong>{friendlyUpstream}</strong> did not produce a list in its last
                run. Adjust its instructions so it returns a list, then run the workflow
                again.
            </div>
        );
    }

    if (choiceOptions.length === 1) {
        const only = choiceOptions[0];
        return (
            <div className="loop-picker-card loop-picker-card--ok">
                Looping over <strong>{only.label}</strong> from {friendlyUpstream}
                <span className="loop-picker-muted">{` (${only.count} item${only.count === 1 ? '' : 's'})`}</span>.
            </div>
        );
    }

    // Multiple lists — present a structured choice. The visible value is the
    // friendly label; the stored value is still the dotted path.
    const activeChoice = choiceOptions.find((opt) => opt.path === value) || choiceOptions[0];
    return (
        <>
            <select
                className="form-select"
                value={activeChoice.path}
                onChange={(e) => {
                    autoAssignedRef.current = e.target.value;
                    onChange(e.target.value);
                }}
            >
                {choiceOptions.map((opt) => (
                    <option key={opt.path} value={opt.path}>
                        {`${opt.label} (${opt.count} item${opt.count === 1 ? '' : 's'})`}
                    </option>
                ))}
            </select>
            <span className="form-hint">
                {friendlyUpstream} produced {choiceOptions.length} lists in its last run.
                Pick the one this loop should iterate.
            </span>
        </>
    );
}

export default LoopItemsPicker;
