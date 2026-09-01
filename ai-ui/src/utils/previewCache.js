// SPDX-License-Identifier: Apache-2.0
/**
 * Preview Cache — Browser Cache API wrapper for uploaded file previews.
 *
 * Files are stored entirely in the user's browser via the Cache API.
 *
 * Flow:
 *   1. User uploads a file → Chat.jsx reads the File object
 *   2. cacheStore() saves the raw bytes into the browser cache
 *   3. User clicks preview → cachedGet() retrieves from cache
 *   4. cachePurgeExpired() cleans entries older than MAX_AGE_MS on app load
 *
 * Cache behaviour:
 *   - Cache name:  "ainxt-preview-cache"
 *   - Max age:     7 days (configurable)
 *   - Storage:     browser-only, no server involvement
 *   - Eviction:    browser may evict under storage pressure — user can re-upload
 */

const CACHE_NAME       = "ainxt-preview-cache";
const MAX_AGE_MS       = 7 * 24 * 60 * 60 * 1000;  // 7 days
const TIMESTAMP_HEADER = "x-cached-at";

/**
 * Build a consistent cache key for a given attachment ID.
 * Uses a synthetic URL so the Cache API can key on it.
 */
export function cacheKey(attachmentId) {
  return `/_preview_cache_/${attachmentId}`;
}

/**
 * Check if Cache API is available in this browser.
 */
function isCacheAvailable() {
  return typeof caches !== "undefined";
}

/**
 * Store a file blob in the browser cache (called at upload time).
 *
 * @param {string} attachmentId — the attachment UUID
 * @param {Blob}   blob         — the raw file bytes
 * @param {string} contentType  — MIME type (e.g. "application/pdf")
 */
export async function cacheStore(attachmentId, blob, contentType) {
  if (!isCacheAvailable()) return;

  try {
    const cache   = await caches.open(CACHE_NAME);
    const headers = new Headers({
      "Content-Type":     contentType || "application/octet-stream",
      [TIMESTAMP_HEADER]: String(Date.now()),
    });
    const response = new Response(blob, { status: 200, statusText: "OK", headers });
    await cache.put(cacheKey(attachmentId), response);
  } catch (e) {
    console.error("[previewCache] Failed to store file in browser cache:", e);
  }
}

/**
 * Retrieve a cached file response from the browser cache.
 * Returns a Response object if found and fresh, or null if missing/expired.
 *
 * @param {string} attachmentId — the attachment UUID
 * @returns {Promise<Response|null>}
 */
export async function cachedGet(attachmentId) {
  if (!isCacheAvailable()) return null;

  try {
    const cache  = await caches.open(CACHE_NAME);
    const cached = await cache.match(cacheKey(attachmentId));
    if (!cached) return null;

    const cachedAt = Number(cached.headers.get(TIMESTAMP_HEADER) || 0);
    if (cachedAt && (Date.now() - cachedAt) >= MAX_AGE_MS) {
      // Expired — remove and return null
      await cache.delete(cacheKey(attachmentId));
      return null;
    }

    return cached;
  } catch (e) {
    console.error("[previewCache] Failed to retrieve file from browser cache:", e);
    return null;
  }
}

/**
 * Cache-first, server-fallback retrieval.
 *
 * Tries the browser cache; on a miss (evicted / expired / different device or
 * browser) fetches the bytes from the authenticated server endpoint
 * GET /chat/attachments/{id}/raw and re-populates the local cache so
 * subsequent reads are fast. Returns a Response (so callers can use
 * .blob()/.arrayBuffer()/.text() exactly like cachedGet), or null if the
 * bytes are unavailable both locally and on the server.
 *
 * The server is now the source of truth for uploaded documents/images;
 * the Cache API is only a fast local layer. This makes previews survive
 * re-login, browser restart, and cross-device access.
 *
 * @param {string} attachmentId — the attachment UUID
 * @returns {Promise<Response|null>}
 */
export async function cachedGetOrFetch(attachmentId) {
  // 1) Fast path — local cache.
  const local = await cachedGet(attachmentId);
  if (local) return local;

  // 2) Fallback — authenticated server fetch (JWT httpOnly cookie).
  try {
    const { API_BASE, authFetch } = await import("../config");
    const res = await authFetch(`${API_BASE}/chat/attachments/${attachmentId}/raw`);
    if (!res || !res.ok) return null;

    const contentType = res.headers.get("Content-Type") || "application/octet-stream";
    const blob = await res.blob();

    // Re-populate the local cache for subsequent fast reads.
    await cacheStore(attachmentId, blob, contentType);

    // Return a fresh Response so callers can consume the body.
    return new Response(blob, {
      status: 200,
      statusText: "OK",
      headers: new Headers({ "Content-Type": contentType }),
    });
  } catch (e) {
    console.error("[previewCache] Server fallback fetch failed:", e);
    return null;
  }
}

/**
 * Remove a specific entry from the preview cache.
 *
 * @param {string} attachmentId — the attachment UUID
 */
export async function cacheRemove(attachmentId) {
  if (!isCacheAvailable()) return;
  try {
    const cache = await caches.open(CACHE_NAME);
    await cache.delete(cacheKey(attachmentId));
  } catch (e) {
    console.error("[previewCache] Failed to remove file from browser cache:", e);
  }
}

/**
 * Purge all expired entries from the preview cache.
 * Call on app load to keep cache size in check.
 */
export async function cachePurgeExpired() {
  if (!isCacheAvailable()) return;
  try {
    const cache = await caches.open(CACHE_NAME);
    const keys  = await cache.keys();
    const now   = Date.now();
    for (const request of keys) {
      const response = await cache.match(request);
      if (!response) continue;
      const cachedAt = Number(response.headers.get(TIMESTAMP_HEADER) || 0);
      if (!cachedAt || (now - cachedAt) >= MAX_AGE_MS) {
        await cache.delete(request);
      }
    }
  } catch (e) {
    console.error("[previewCache] Failed to purge expired entries from browser cache:", e);
  }
}
