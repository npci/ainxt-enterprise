// SPDX-License-Identifier: Apache-2.0
// Resolution order:
//   1. VITE_API_BASE_URL env var (set at build time)
//   2. Standalone ABStudio dev server (port 5174): /api, caught by its own
//      Vite proxy and rewritten to /ainxt/v1/api/abs on the gateway.
//   3. Embedded in ai-ui / production: /ainxt/v1/api/abs, caught by ai-ui's
//      Vite proxy or served directly by the gateway-mounted ABStudio routers.
const isStandaloneDev = import.meta.env.DEV && window.location.port === '5174';
export const API_BASE = import.meta.env.VITE_API_BASE_URL || (isStandaloneDev ? '/api' : '/ainxt/v1/api/abs');

// Platform endpoints (Knowledge Base, departments, etc.) live on the main
// backend, NOT ABStudio's own API_BASE. In the embedded (ai-ui) deployment
// they're routed by the same Vite proxy rule that catches /ainxt/v1/api/*.
// JWT auth uses an httpOnly cookie the browser sends automatically when
// fetch is called with `credentials: 'include'`.
export const PLATFORM_API_BASE = import.meta.env.VITE_PLATFORM_API_BASE_URL || '/ainxt/v1/api';
export const KB_API_BASE = `${PLATFORM_API_BASE}/kb`;

// Shared body for cookie-auth fetch wrappers. JSON is the default content
// type; pass ``omitContentType: true`` for multipart FormData uploads so
// the browser sets the boundary itself. Returns the raw Response.
function _cookieFetch(base, path, options = {}) {
    const { omitContentType, headers: extraHeaders, ...rest } = options;
    const headers = { ...(extraHeaders || {}) };
    if (!omitContentType && !('Content-Type' in headers)) {
        headers['Content-Type'] = 'application/json';
    }
    return fetch(`${base}${path}`, {
        cache: 'no-store',
        credentials: 'include',
        ...rest,
        headers,
    });
}

/**
 * Authenticated fetch helper for ANY platform endpoint under /ainxt/v1/api/*.
 *
 * @param {string} path - relative path under PLATFORM_API_BASE, e.g. '/kb/upload'
 * @param {RequestInit} [options]
 */
export function platformFetch(path, options = {}) {
    return _cookieFetch(PLATFORM_API_BASE, path, options);
}

/**
 * Convenience wrapper for the /kb subtree — paths are relative to KB_API_BASE
 * (e.g. '/upload', '/namespaces').
 */
export function kbFetch(path, options = {}) {
    return platformFetch(`/kb${path}`, options);
}

/**
 * Authenticated fetch helper for ABStudio's own backend (``API_BASE``).
 *
 * Use this — not ``apiFetch`` — for multipart uploads: ``apiFetch`` applies
 * an 8 s AbortController timeout and JSON-parses every response, neither
 * of which suits the Build Studio KB uploader (embedding can exceed 10 s
 * and we want the raw Response so the caller can read status codes).
 *
 * @param {string} path - relative path under API_BASE, e.g. '/kb/upload-build-studio'
 * @param {RequestInit} [options]
 */
export function absFetch(path, options = {}) {
    return _cookieFetch(API_BASE, path, options);
}

/**
 * Build headers for API requests.
 * No authentication required — this is a standalone local tool.
 *
 * Pass `{ omitContentType: true }` for FormData / multipart uploads —
 * the browser MUST set the Content-Type itself so it can include the
 * correct multipart boundary string.
 */
export function buildAuthHeaders(extra = {}) {
    const { omitContentType, ...rest } = extra;
    if (omitContentType) return { ...rest };
    return {
        'Content-Type': 'application/json',
        ...rest,
    };
}

/**
 * Shared fetch helper used by all Zustand stores.
 *
 * Features:
 *   • AbortController timeout (default 8 s, pass `timeoutMs` to override)
 *   • Parses JSON error bodies and surfaces `detail` field
 *   • Returns `null` for 204 No Content responses
 *   • Throws a friendly message on timeout
 *
 * @param {string} path - API path relative to API_BASE (e.g. '/agents')
 * @param {RequestInit} options - fetch options (method, body, headers, …)
 * @param {number} [timeoutMs=8000] - abort timeout in milliseconds
 * @returns {Promise<any>}
 */
export async function apiFetch(path, options = {}, timeoutMs = 8000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    if (!path.startsWith('/')) throw new Error('Invalid API path');
    const sanitizedPath = path.replace(/[^a-zA-Z0-9/_\-%.=?&]/g, '');
    const targetUrl = `${API_BASE}${sanitizedPath}`;
    try {
        const res = await fetch(targetUrl, {
            headers: buildAuthHeaders(),
            signal: controller.signal,
            ...options,
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            // ``detail`` may be either a string ("not found") or a structured
            // object like ``{error: "invalid_name", message: "Workflow name …"}``
            // (used for validation failures so the UI can switch on the code).
            let message;
            if (typeof err.detail === 'string') {
                message = err.detail;
            } else if (err.detail && typeof err.detail === 'object') {
                message = err.detail.message || err.detail.error || `Request failed: ${res.status}`;
            } else {
                message = `Request failed: ${res.status}`;
            }
            const error = new Error(message);
            error.status = res.status;
            error.detail = err.detail;
            throw error;
        }
        if (res.status === 204) return null;
        return res.json();
    } catch (e) {
        if (e.name === 'AbortError') throw new Error('Backend not responding — is it running on port 8000?');
        throw e;
    } finally {
        clearTimeout(timer);
    }
}
