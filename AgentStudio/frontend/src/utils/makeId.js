// SPDX-License-Identifier: MIT
/**
 * Short locally-unique id used for transient client-side entities (chat
 * temp ids, condition/case rows, attachments, etc.). Date.now() alone
 * collides when generated inside the same millisecond — the random
 * suffix makes that safe.
 */
export function makeId(prefix) {
    const rand = Math.random().toString(36).slice(2, 8);
    return prefix ? `${prefix}-${Date.now()}-${rand}` : `${Date.now()}-${rand}`;
}

/** Return the first value in `arr` that appears more than once, or null. */
export function findDuplicate(arr) {
    const seen = new Set();
    for (const v of arr) {
        if (seen.has(v)) return v;
        seen.add(v);
    }
    return null;
}
