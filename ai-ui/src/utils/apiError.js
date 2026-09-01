// SPDX-License-Identifier: Apache-2.0
// ============================================================
// apiError — safely extract a human-readable message from any
// FastAPI-shaped error response.
// ============================================================
//
// FastAPI's `detail` field on an error response can be:
//   - a plain string                      -> our own HTTPException(detail="...")
//     e.g. {"detail": "value: Token value cannot be empty"}
//   - a list of Pydantic validation errors -> raised automatically by
//     FastAPI/Pydantic for request-body validation failures (missing
//     field, bad Literal/enum value, wrong type, etc.) *before* any of
//     our own request handlers or validators ever run.
//     e.g. {"detail": [{"type": "missing", "loc": ["body", "value"],
//                        "msg": "Field required", "input": {...}}]}
//   - absent entirely (network error, non-JSON body, etc.)
//
// Code across the app has historically done `new Error(json.detail || "Failed")`
// and then shown `err.message` in a banner. When `detail` is the Pydantic
// list shape above, `new Error(list)` stringifies the array to the literal
// text "[object Object]" (Array.prototype.toString joins Object.toString()
// results), which is what a user sees instead of a real explanation.
//
// extractErrorMessage() normalises all three shapes into a single readable
// string, so it's always safe to pass its result straight to `new Error(...)`
// or a setError()-style state setter.

/**
 * Turn a single Pydantic validation-error object into a short human phrase.
 * @param {{loc?: (string|number)[], msg?: string}} item
 */
function formatPydanticError(item) {
  if (!item || typeof item !== "object") return String(item);
  const loc = Array.isArray(item.loc)
    ? item.loc.filter(p => p !== "body").join(".")
    : "";
  const msg = item.msg || item.type || "Invalid value";
  return loc ? `${loc}: ${msg}` : msg;
}

/**
 * Extract a readable error message from a parsed JSON error body (the
 * result of `await res.json()`), falling back to `fallback` if nothing
 * usable is found.
 *
 * @param {any} body - parsed JSON response body (may be null/undefined)
 * @param {string} fallback - message to use when body has no usable detail
 * @returns {string}
 */
export function extractErrorMessage(body, fallback = "Request failed") {
  const detail = body?.detail;
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map(formatPydanticError).filter(Boolean);
    return parts.length ? parts.join("; ") : fallback;
  }
  // Any other shape (object, number, etc.) — avoid ever handing a raw
  // object to `new Error()` / a banner, which renders as "[object Object]".
  if (typeof detail === "object") {
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return String(detail);
}

/**
 * Convenience wrapper for the extremely common
 *   `if (!res.ok) throw new Error((await res.json()).detail || "Failed")`
 * pattern — parses the response body defensively (it may not be JSON at
 * all, e.g. an HTML error page from a proxy) and returns a ready-to-throw
 * Error with a guaranteed-readable message.
 *
 * @param {Response} res - a fetch Response with `res.ok === false`
 * @param {string} fallback
 * @returns {Promise<Error>}
 */
export async function apiErrorFromResponse(res, fallback = "Request failed") {
  let body = null;
  try {
    body = await res.json();
  } catch {
    // Non-JSON body (proxy/HTML error page, empty body, etc.) — fall
    // through to the generic fallback below.
  }
  return new Error(extractErrorMessage(body, fallback));
}
