// SPDX-License-Identifier: Apache-2.0
/**
 * Shared utility functions for trigger-related components.
 * Extracted from TriggerSection.jsx and TriggerNotifications.jsx to avoid
 * duplication.
 */

/**
 * Format an ISO timestamp as a human-readable IST string.
 * @param {string} iso - ISO 8601 timestamp
 * @returns {string}
 */
export function formatIst(iso) {
    if (!iso) return '';
    try {
        const d = new Date(iso);
        return d.toLocaleString('en-IN', {
            timeZone: 'Asia/Kolkata',
            year: 'numeric', month: 'short', day: '2-digit',
            hour: '2-digit', minute: '2-digit', hour12: true,
        }) + ' IST';
    } catch {
        return iso;
    }
}

/**
 * Format an ISO timestamp as a short IST string (no year).
 * Used in compact contexts like the notification bell.
 * @param {string} iso - ISO 8601 timestamp
 * @returns {string}
 */
export function formatIstShort(iso) {
    if (!iso) return '';
    try {
        return new Date(iso).toLocaleString('en-IN', {
            timeZone: 'Asia/Kolkata',
            month: 'short', day: '2-digit',
            hour: '2-digit', minute: '2-digit', hour12: true,
        }) + ' IST';
    } catch {
        return iso;
    }
}

/**
 * Return a human-readable duration between two ISO timestamps.
 * @param {string} startIso
 * @param {string} endIso
 * @returns {string}
 */
export function durationLabel(startIso, endIso) {
    try {
        const ms = new Date(endIso) - new Date(startIso);
        if (ms < 1000) return `${ms}ms`;
        if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
        return `${Math.round(ms / 60_000)}m`;
    } catch {
        return '';
    }
}
