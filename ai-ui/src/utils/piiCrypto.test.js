// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Same 32-byte AES-256-GCM key format as core/pii_crypto.py's PII_ENCRYPTION_KEY
// (URL-safe base64). Test-only value — never used outside this file.
const TEST_KEY_B64URL = "3TFWAEcGvCmU-suZO44Q6skyL1JPus22_4BJSgPcHMc=".replace(/=+$/, "");

// piiCrypto.js caches the imported CryptoKey and its "warned once" flag at
// module scope, and it reads VITE_PII_ENCRYPTION_KEY lazily. Both the
// key-present and key-absent scenarios therefore need a *fresh* module
// instance, so every test imports the module through this helper after
// setting the env var it wants.
//
// The env var is stubbed explicitly rather than inherited from the ambient
// environment. vite.config.js sets envDir to the repo root so the real
// VITE_PII_ENCRYPTION_KEY in ../.env is inlined into production bundles —
// which means "no key" is no longer the default in this process, and any test
// that silently relied on that would assert the opposite of what it claims.
async function loadModule({ key } = {}) {
  vi.resetModules();
  if (key === undefined) {
    vi.stubEnv("VITE_PII_ENCRYPTION_KEY", "");
  } else {
    vi.stubEnv("VITE_PII_ENCRYPTION_KEY", key);
  }
  return import("./piiCrypto.js");
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("piiCrypto — flag gating (no key needed)", () => {
  it("encrypt is a no-op when disabled", async () => {
    const { encryptPii } = await loadModule({ key: TEST_KEY_B64URL });
    expect(await encryptPii("alice@example.com", false)).toBe("alice@example.com");
  });

  it("decrypt is a no-op when disabled", async () => {
    const { decryptPii } = await loadModule({ key: TEST_KEY_B64URL });
    expect(await decryptPii("pii:v1:whatever", false)).toBe("pii:v1:whatever");
  });

  it("encrypt is a no-op for falsy values even when enabled", async () => {
    const { encryptPii } = await loadModule({ key: TEST_KEY_B64URL });
    expect(await encryptPii("", true)).toBe("");
    expect(await encryptPii(null, true)).toBe(null);
  });

  it("decrypt passes through a value without the pii:v1: prefix unchanged", async () => {
    const { decryptPii } = await loadModule({ key: TEST_KEY_B64URL });
    expect(await decryptPii("plain@example.com", true)).toBe("plain@example.com");
  });
});

describe("piiCrypto — key configured (the shipping configuration)", () => {
  it("round-trips a value through encrypt -> decrypt", async () => {
    const { encryptPii, decryptPii } = await loadModule({ key: TEST_KEY_B64URL });
    const enc = await encryptPii("alice@example.com", true);
    expect(enc).toMatch(/^pii:v1:/);
    expect(enc).not.toContain("alice@example.com");
    expect(await decryptPii(enc, true)).toBe("alice@example.com");
  });

  it("emits canonical base64url — no padding, no + or / characters", async () => {
    // core/pii_crypto.py re-adds the stripped padding before decoding, so the
    // wire format must stay padding-free for the backend to accept it.
    const { encryptPii } = await loadModule({ key: TEST_KEY_B64URL });
    const token = (await encryptPii("alice@example.com", true)).slice("pii:v1:".length);
    expect(token).not.toMatch(/[+/=]/);
  });

  it("uses a fresh nonce per call, so the same input yields different ciphertext", async () => {
    const { encryptPii, decryptPii } = await loadModule({ key: TEST_KEY_B64URL });
    const a = await encryptPii("alice@example.com", true);
    const b = await encryptPii("alice@example.com", true);
    expect(a).not.toBe(b);
    expect(await decryptPii(a, true)).toBe("alice@example.com");
    expect(await decryptPii(b, true)).toBe("alice@example.com");
  });

  it("piiKeyMissing is false when a valid key is present", async () => {
    const { piiKeyMissing } = await loadModule({ key: TEST_KEY_B64URL });
    expect(piiKeyMissing(true)).toBe(false);
    expect(piiKeyMissing(false)).toBe(false);
  });

  it("a wrong-but-valid key fails safe instead of leaking ciphertext", async () => {
    // Simulates a rotated server key: the token decodes but the GCM tag check
    // fails. The UI must show the placeholder, never the raw token.
    const good = await loadModule({ key: TEST_KEY_B64URL });
    const token = await good.encryptPii("alice@example.com", true);

    const otherKey = Buffer.from(new Uint8Array(32).fill(7))
      .toString("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
    const rotated = await loadModule({ key: otherKey });

    const out = await rotated.decryptPii(token, true);
    expect(out).toBe(rotated.PII_UNAVAILABLE);
    expect(out).not.toContain("pii:v1:");
  });

  it("ignores a key that is not 32 bytes and reports the build as broken", async () => {
    const shortKey = Buffer.from(new Uint8Array(16).fill(3))
      .toString("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
    const { piiKeyMissing } = await loadModule({ key: shortKey });
    expect(piiKeyMissing(true)).toBe(true);
  });
});

describe("piiCrypto — key missing (misconfigured build)", () => {
  it("encrypt is a safe no-op so a missing key never blocks a request", async () => {
    const { encryptPii } = await loadModule();
    expect(await encryptPii("bob@example.com", true)).toBe("bob@example.com");
  });

  it("decrypt returns a placeholder and NEVER leaks ciphertext", async () => {
    // Regression guard: returning the input here is what rendered raw
    // "pii:v1:..." tokens in place of the user's name and email.
    const { decryptPii, PII_UNAVAILABLE } = await loadModule();
    const dec = await decryptPii("pii:v1:not-real-ciphertext", true);
    expect(dec).toBe(PII_UNAVAILABLE);
    expect(dec).not.toContain("pii:v1:");
  });

  it("decrypt never throws on a malformed pii:v1: payload — fails safe", async () => {
    const { decryptPii, PII_UNAVAILABLE } = await loadModule();
    const out = await decryptPii("pii:v1:!!!not-base64!!!", true);
    expect(out).toBe(PII_UNAVAILABLE);
    expect(out).not.toContain("pii:v1:");
  });

  it("piiKeyMissing reports a misconfigured build only when encryption is on", async () => {
    const { piiKeyMissing } = await loadModule();
    expect(piiKeyMissing(true)).toBe(true);
    expect(piiKeyMissing(false)).toBe(false);
  });
});
