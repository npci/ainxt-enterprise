// SPDX-License-Identifier: MIT
/**
 * Helpers for the Loop node's connection-aware list picker.
 *
 * Pathing convention mirrors the engine's `_run_loop`:
 *   `input`              → the whole upstream value
 *   `input.<key>.<key>`  → walk a nested object
 */

const MAX_DEPTH        = 4;
const MAX_LISTS        = 12;
const MAX_NODES_VISITED = 5000;
const SAMPLE_PREVIEW   = 80;

/**
 * The cached output is stored server-side as a plain string. Most agents emit
 * JSON; some emit prose. Return whatever we can parse, falling back to the
 * raw string so the picker can still render a "this is text" hint.
 */
export function parseUpstreamOutput(raw) {
    if (raw == null) return null;
    if (typeof raw !== 'string') return raw;
    const trimmed = raw.trim();
    if (!trimmed) return '';
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
        try { return JSON.parse(trimmed); } catch { /* fall through */ }
    }
    return raw;
}

function previewSample(item) {
    if (item == null) return '';
    if (typeof item === 'string') {
        return item.length > SAMPLE_PREVIEW
            ? item.slice(0, SAMPLE_PREVIEW) + '...'
            : item;
    }
    if (typeof item === 'number' || typeof item === 'boolean') return String(item);
    try {
        const s = JSON.stringify(item);
        return s.length > SAMPLE_PREVIEW ? s.slice(0, SAMPLE_PREVIEW) + '...' : s;
    } catch {
        return '';
    }
}

/**
 * Walk a JS value and collect every array, paired with the dotted path that
 * addresses it.
 *
 * @param {unknown} value — parsed upstream output
 * @returns {Array<{path: string, length: number, samplePreview: string}>}
 */
export function findListsInOutput(value) {
    const found = [];
    let visited = 0;

    function walk(node, path, depth) {
        if (found.length >= MAX_LISTS || depth > MAX_DEPTH || visited >= MAX_NODES_VISITED) return;
        visited++;
        if (Array.isArray(node)) {
            found.push({
                path,
                length: node.length,
                samplePreview: previewSample(node[0]),
            });
            // Don't dive into list items — usually noise.
            return;
        }
        if (node && typeof node === 'object') {
            for (const key of Object.keys(node)) {
                if (found.length >= MAX_LISTS || visited >= MAX_NODES_VISITED) break;
                walk(node[key], `${path}.${key}`, depth + 1);
            }
        }
    }

    walk(value, 'input', 0);
    return found;
}

/**
 * Given the workflow's edges and a Loop node's id, return the id of the node
 * whose output feeds the loop's top input — or null if nothing is connected.
 *
 * Loop top-input handle id is `'target'` (see LoopNode.jsx). We accept null /
 * empty `targetHandle` too because some older edges were created before the
 * handle id was added.
 */
export function getUpstreamNodeId(edges, loopNodeId) {
    if (!loopNodeId || !Array.isArray(edges)) return null;
    const edge = edges.find((e) => {
        if (e.target !== loopNodeId) return false;
        const handle = e.targetHandle || '';
        return handle === '' || handle === 'target';
    });
    return edge ? edge.source : null;
}
