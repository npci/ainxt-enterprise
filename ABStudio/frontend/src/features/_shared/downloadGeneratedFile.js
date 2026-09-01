// SPDX-License-Identifier: Apache-2.0
// Download helper for `/generated-files/<filename>`.
// The backend keeps the file for a TTL window (default 24h). After it
// expires the endpoint returns 410. Fetching through this helper lets
// the caller distinguish 410 ('gone') from real errors and avoid the
// browser navigating to raw JSON.
//
// Returns: { status: 'ok' } | { status: 'gone', message } | { status: 'error', message }
import { API_BASE, buildAuthHeaders } from '../../config/api';

// Turn a bare `/generated-files/<name>` path into an absolute URL. Already
// absolute inputs (http(s):// or API_BASE-prefixed) pass through unchanged.
export function resolveGeneratedUrl(rawUrl) {
    if (!rawUrl) return '';
    if (rawUrl.startsWith('http') || rawUrl.startsWith(API_BASE)) return rawUrl;
    return `${API_BASE}${rawUrl}`;
}

export async function downloadGeneratedFile(url, filename) {
    let res;
    try {
        res = await fetch(url, { headers: buildAuthHeaders() });
    } catch (err) {
        return { status: 'error', message: err?.message || 'Network error' };
    }

    if (res.status === 410) {
        let detail = 'This file has expired and is no longer available.';
        try {
            const body = await res.json();
            if (body && body.detail) detail = body.detail;
        } catch { /* non-JSON body — keep the default */ }
        return { status: 'gone', message: detail };
    }

    if (!res.ok) {
        let detail = `Download failed (${res.status})`;
        try {
            const body = await res.json();
            if (body && body.detail) detail = body.detail;
        } catch { /* ignore */ }
        return { status: 'error', message: detail };
    }

    // Stream the body into a blob and trigger the browser save UX.
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    try {
        const a = document.createElement('a');
        a.href = objectUrl;
        a.download = filename || 'download';
        document.body.appendChild(a);
        a.click();
        a.remove();
    } finally {
        // Give the browser a tick to start the save before revoking.
        setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    }
    return { status: 'ok' };
}
