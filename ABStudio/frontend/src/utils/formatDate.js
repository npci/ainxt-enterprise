// SPDX-License-Identifier: Apache-2.0
/**
 * Human-friendly date formatting shared by AgentCard, WorkflowCard, and the
 * Skills tab (Created by / Approved by lines).
 *
 * Rendered in Asia/Kolkata (IST) explicitly so:
 *   - the output is stable regardless of the viewer's OS timezone
 *   - a UTC-serialized backend value (e.g. ``2026-08-04T05:27:45+00:00``)
 *     shows the correct IST wall-clock ("10:57 AM"), never the raw UTC hour
 *
 * A backend value serialized WITHOUT a timezone suffix (naive ISO string) is
 * assumed to be UTC — the same convention used across the platform. Without
 * this assumption, JS would parse it as browser-local, producing a 5:30h
 * shift on IST machines.
 */
export default function formatDate(dateString) {
    if (!dateString) return '';
    let ts = dateString;
    if (typeof ts === 'string') {
        // Append "Z" if the string carries no tz suffix so Date() parses it
        // as UTC, not local. Guards against writers that emit naive ISO
        // strings from ``datetime.utcnow().isoformat()``.
        const hasTZ = /[zZ]|[+-]\d{2}:?\d{2}$/.test(ts);
        if (!hasTZ) ts = ts + 'Z';
    }
    const date = new Date(ts);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleString('en-US', {
        timeZone: 'Asia/Kolkata',
        month: 'short', day: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true,
    });
}
