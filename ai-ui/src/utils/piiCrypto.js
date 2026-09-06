// SPDX-License-Identifier: MIT
// ============================================================
// piiCrypto — browser-side counterpart to core/pii_crypto.py.
//
// Encrypts/decrypts the same sensitive fields (email, name, phone/mobile)
// the backend already wraps with encrypt_pii()/decrypt_pii(), so a value
// can round-trip browser -> backend -> browser without ever sitting in
// plaintext in a request/response body, in transit or in any
// log/proxy/cache sitting between the two.
//
// Wire format — MUST match core/pii_crypto.py exactly:
//   "pii:v1:" + base64url(12-byte nonce || AES-256-GCM ciphertext+tag)
//
// Gating: mirrors PII_PAYLOAD_ENCRYPTION_ENABLED — driven by the
// `pii_payload_encryption_enabled` flag from GET /auth/ui-config (a
// runtime backend flag, not a build-time constant, so a deployment can
// flip it without rebuilding the frontend). When disabled (default),
// encryptPii()/decryptPii() are no-ops — behavior is byte-for-byte
// identical to not calling this module at all.
//
// Key: VITE_PII_ENCRYPTION_KEY — same URL-safe base64, 32-byte value as
// the backend's PII_ENCRYPTION_KEY. Baked into the JS bundle at build
// time (Vite). This protects data while it is in transit or sitting in
// logs/proxies/intermediate services — it does NOT hide data from
// someone with access to this browser session's devtools, since the
// browser must hold the key to decrypt what it receives.
// ============================================================

const PII_PREFIX = "pii:v1:";
const NONCE_LEN = 12; // 96-bit GCM nonce, same as core/pii_crypto.py

/**
 * Rendered in place of a value that cannot be decrypted. Deliberately NOT the
 * ciphertext: showing "pii:v1:3Yd0MH-..." where a name belongs looks like
 * corrupted data to the user, and in an editable input it can be saved back,
 * overwriting the real value in the database.
 */
export const PII_UNAVAILABLE = "\u2014"; // em dash

let _cryptoKeyPromise = null;

// base64 (standard, with +/=) <-> base64url (with -_ , no padding) —
// JS btoa/atob only speak standard base64; Python's urlsafe_b64encode
// speaks base64url. Convert both ways so ciphertext round-trips exactly.
function _b64ToB64Url(b64) {
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function _b64UrlToB64(b64url) {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
  const pad = b64.length % 4 === 0 ? "" : "=".repeat(4 - (b64.length % 4));
  return b64 + pad;
}

function _bytesToB64(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
function _b64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

/** Get the raw 32-byte key from VITE_PII_ENCRYPTION_KEY (base64url), or null if unset. */
function _rawKeyBytes() {
  const keyB64Url = import.meta.env.VITE_PII_ENCRYPTION_KEY;
  if (!keyB64Url) return null;
  try {
    const bytes = _b64ToBytes(_b64UrlToB64(keyB64Url.trim()));
    return bytes.length === 32 ? bytes : null;
  } catch {
    return null;
  }
}

function _importKey() {
  if (!_cryptoKeyPromise) {
    const raw = _rawKeyBytes();
    _cryptoKeyPromise = raw
      ? crypto.subtle.importKey("raw", raw, { name: "AES-GCM" }, false, ["encrypt", "decrypt"])
      : Promise.resolve(null);
  }
  return _cryptoKeyPromise;
}

// Warn once, not once per field. A page paints dozens of PII values, so an
// unconditional log would flood the console and bury the actual message.
let _warnedNoKey = false;
function _warnMisconfigured(detail) {
  if (_warnedNoKey) return;
  _warnedNoKey = true;
  console.error(
    `[piiCrypto] ${detail} The server sent PII-encrypted fields ` +
    "(PII_PAYLOAD_ENCRYPTION_ENABLED=true) but this build cannot decrypt them, " +
    "so raw \"pii:v1:...\" ciphertext would be displayed. Rebuild the frontend " +
    "with VITE_PII_ENCRYPTION_KEY set to the same value as the server's " +
    "PII_ENCRYPTION_KEY."
  );
}

/**
 * True when the server is sending encrypted PII but this build has no usable
 * key — i.e. every decryptPii() call is about to pass ciphertext through to
 * the UI. Lets a screen show an explicit error instead of rendering
 * "pii:v1:..." as if it were the user's name.
 */
export function piiKeyMissing(enabled) {
  return !!enabled && _rawKeyBytes() === null;
}

/**
 * Encrypt *value* for placement into an outgoing request payload.
 * No-op (returns *value* unchanged) when the flag is disabled, no key is
 * configured, or the value is falsy — mirrors core/pii_crypto.py::encrypt_pii.
 */
export async function encryptPii(value, enabled) {
  if (!value || !enabled) return value;
  const key = await _importKey();
  if (!key) return value;
  try {
    const nonce = crypto.getRandomValues(new Uint8Array(NONCE_LEN));
    const ciphertext = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv: nonce }, key, new TextEncoder().encode(value)
    );
    const combined = new Uint8Array(NONCE_LEN + ciphertext.byteLength);
    combined.set(nonce, 0);
    combined.set(new Uint8Array(ciphertext), NONCE_LEN);
    return PII_PREFIX + _b64ToB64Url(_bytesToB64(combined));
  } catch {
    return value; // never block a request on a client-side crypto failure
  }
}

/**
 * Decrypt a value previously produced by encrypt_pii() (backend or this
 * module). Safe no-op (returns *value* unchanged) when the flag is
 * disabled, no key is configured, the value is falsy, or it doesn't carry
 * the pii:v1: prefix (already plaintext) — mirrors core/pii_crypto.py's
 * decrypt_pii. Any decryption failure (wrong/rotated key) also falls back
 * to the original value rather than throwing, so a stale key never crashes
 * the UI.
 */
export async function decryptPii(value, enabled) {
  if (!value || !enabled || !value.startsWith(PII_PREFIX)) return value;
  const key = await _importKey();
  if (!key) {
    // No key configured. Returning `value` here is what put raw
    // "pii:v1:..." tokens on screen in place of names and email addresses,
    // so surface a neutral placeholder instead and log the real cause once.
    _warnMisconfigured("VITE_PII_ENCRYPTION_KEY is not set or is not a valid 32-byte base64url key.");
    return PII_UNAVAILABLE;
  }
  try {
    const token = value.slice(PII_PREFIX.length);
    const raw = _b64ToBytes(_b64UrlToB64(token));
    const nonce = raw.slice(0, NONCE_LEN);
    const ciphertext = raw.slice(NONCE_LEN);
    const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv: nonce }, key, ciphertext);
    return new TextDecoder().decode(plaintext);
  } catch {
    // Wrong/rotated key or corrupt payload. Never fall back to `value` — that
    // leaks ciphertext into the UI and, in an editable field, risks the user
    // saving it over their real data.
    _warnMisconfigured("A pii:v1: value failed to decrypt (wrong or rotated key).");
    return PII_UNAVAILABLE;
  }
}
