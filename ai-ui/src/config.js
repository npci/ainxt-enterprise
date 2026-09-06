// SPDX-License-Identifier: MIT
/**
 * API base URL — all API calls go under /ainxt/v1/api
 *
 * Local dev  (Vite dev server at :5173)
 *   → fetch('/ainxt/v1/api/agents') hits Vite proxy → forwarded to FastAPI at :8000
 *
 * Production  (nginx at :443 proxying /ainxt/v1/api/* to backend pool)
 *   → Single upstream location block: proxy_pass http://ainxt_backend
 */
export const API_BASE = '/ainxt/v1/api';



// SPA mount prefix — Vite production base, BrowserRouter basename, and the
// URL tamper guard in main.jsx all derive from this single constant.
export const PORTAL_BASE = '/portal';

// ─── MODEL DEFAULTS ──────────────────────────────────────────────────────────
// One place for the model ids this UI needs before, or instead of, an answer
// from the backend. They were previously scattered as literals across
// CoworkDesktop, Code, Chat, CoachAdmin and imageGenerate -- so a deployment
// running its own models had to find and edit each one, and the copies drifted.
//
// The BACKEND is authoritative: `/auth/ui-config` and the model-governance
// endpoint report what a deployment actually serves. These are the fallbacks
// used until that arrives, and the seed values for the pickers.

// PII encryption key (AES-GCM, base64). Read from ai-ui/.env at build time and
// inlined by Vite into the production bundle.
export const PII_ENCRYPTION_KEY = import.meta.env.VITE_PII_ENCRYPTION_KEY || '';
//
// No model IDs are hardcoded — set these at build time (Vite) for your deployment:
//   VITE_MODEL_DEFAULT=your-primary-model-id
//   VITE_MODEL_DEFAULT_LOCKED=your-locked-model-id
//   VITE_MODEL_IMAGE=your-image-model-id
//   VITE_MODEL_OPUS=your-opus-tier-model-id
//   VITE_MODEL_HAIKU=your-fast-tier-model-id
//   VITE_MODEL_PICKER=model-id:Label,model-id:Label,...
// When blank the UI defers to the live /all-models catalogue from the backend.
const _env = (name, fallback) => (import.meta.env[name] || fallback);

export const MODEL_DEFAULT        = _env('VITE_MODEL_DEFAULT',        '');
export const MODEL_DEFAULT_LOCKED = _env('VITE_MODEL_DEFAULT_LOCKED', '');
export const MODEL_IMAGE          = _env('VITE_MODEL_IMAGE',          '');

// Short alias → id, for the `@model` shorthand users type in the composer.
// Populated from env vars — no model IDs hardcoded.
export const MODEL_ALIASES = Object.freeze({
  sonnet: MODEL_DEFAULT,
  opus:   _env('VITE_MODEL_OPUS',  ''),
  haiku:  _env('VITE_MODEL_HAIKU', ''),
});

// Picker entries as `id:Label` pairs. Kept as one string so a deployment can
// replace the whole list with a single environment variable.
// No models hardcoded — set VITE_MODEL_PICKER at build time.
// Example: VITE_MODEL_PICKER=llama3:Llama 3,mistral:Mistral 7B
const _PICKER_DEFAULT = '';

export const MODEL_PICKER = Object.freeze(
  _env('VITE_MODEL_PICKER', _PICKER_DEFAULT)
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      const idx = entry.indexOf(':');
      const key = idx === -1 ? entry : entry.slice(0, idx);
      const label = idx === -1 ? entry : entry.slice(idx + 1);
      return { key: key.trim(), label: label.trim() };
    })
);

// Presenton-specific configuration (do not change existing exports)
export const PRESENTON_BASE = import.meta.env.VITE_PRESENTON_BASE || '/presenton';
export const ENABLE_PRESENTON = (import.meta.env.VITE_ENABLE_PRESENTON || 'true') === 'true';
// Increased timeout to 6 minutes (360000ms) for prepare endpoint - can be overridden via env
export const PRESENTON_TIMEOUT = Number(import.meta.env.VITE_PRESENTON_TIMEOUT || 360000);
export const PRESENTON_POLL_INTERVAL = Number(import.meta.env.VITE_PRESENTON_POLL_INTERVAL || 3000);
export const PRESENTON_MAX_RETRIES = Number(import.meta.env.VITE_PRESENTON_MAX_RETRIES || 5);

// presentonFetch: use proxy /presenton to reach the Presenton instance through nginx
// Extended timeout for long-running PPT generation (up to 10 minutes)
export function presentonFetch(path, options = {}) {
  const url = path.startsWith('http') ? path : `${PRESENTON_BASE}${path}`;
  
  // Default timeout for PPT operations: 10 minutes
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), options.timeout || PRESENTON_TIMEOUT);
  
  // Merge signal if provided
  if (options.signal) {
    options.signal.addEventListener('abort', () => {
      clearTimeout(timeoutId);
      controller.abort();
    });
  }
  
  return fetch(url, { 
    cache: 'no-store', 
    credentials: 'include',
    signal: controller.signal,
    ...options 
  }).finally(() => clearTimeout(timeoutId));
}
/**
 * Mint a per-request correlation id. Uses crypto.randomUUID when available
 * (all modern browsers over HTTPS/localhost); falls back to crypto.getRandomValues
 * for a cryptographically secure token in legacy/insecure-context paths.
 * Math.random() is intentionally avoided — it is not cryptographically secure.
 */
function _newCorrId() {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  } catch { /* ignore */ }
  // Fallback: build a secure token from two random Uint32 values via getRandomValues
  try {
    if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
      const buf = new Uint32Array(2);
      crypto.getRandomValues(buf);
      return `c-${Date.now().toString(36)}-${buf[0].toString(36)}${buf[1].toString(36)}`;
    }
  } catch { /* ignore */ }
  // Last-resort: timestamp only — no Math.random() to avoid insecure randomness
  return `c-${Date.now().toString(36)}-${performance.now().toString(36).replace('.', '')}`;
}

/**
 * Resolve a URL — prevents double-prefixing when some callers
 * already include API_BASE and others pass bare paths like '/auth/login'.
 */
function _url(path) {
  if (path.startsWith('http') || path.startsWith(API_BASE)) return path;
  return `${API_BASE}${path}`;
}

/**
 * Authenticated fetch wrapper.
 * JWT is stored in an httpOnly cookie — sent automatically for same-origin requests.
 *
 * Phase 6.2 — resilience: transient network blips (fetch *throws*, i.e. no
 * response was ever received) are auto-retried ONCE, but ONLY for idempotent
 * methods (GET/HEAD) and only when the caller hasn't passed its own
 * AbortSignal (streaming/cancelable requests manage their own lifecycle).
 * Non-idempotent methods (POST/PUT/PATCH/DELETE) are never retried here to
 * avoid duplicating a mutation (e.g. sending the same chat message twice).
 */
export async function authFetch(url, options = {}) {
  const opts = { cache: 'no-store', credentials: 'include', ...options };
  opts.headers = { 'x-client-request-id': _newCorrId(), ...(opts.headers || {}) };
  const method = (opts.method || 'GET').toUpperCase();
  const idempotent = method === 'GET' || method === 'HEAD';
  const retryable = idempotent && !opts.signal;
  try {
    return await fetch(_url(url), opts);
  } catch (err) {
    if (!retryable) throw err;
    // Brief backoff, then a single retry. A thrown error means the request
    // never reached the server, so re-issuing a read is safe.
    await new Promise(r => setTimeout(r, 400));
    return fetch(_url(url), opts);
  }
}

/**
 * Unauthenticated fetch wrapper — prevents stale browser caching.
 */
export function apiFetch(url, options = {}) {
  const headers = { 'x-client-request-id': _newCorrId(), ...(options.headers || {}) };
  return fetch(_url(url), { cache: 'no-store', credentials: 'include', ...options, headers });
}
