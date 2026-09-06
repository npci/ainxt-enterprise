// Dev-only known-answer tests for lib/pdf-crypto.js (no framework). Crypto bugs
// produce plausible-looking garbage rather than loud failures, so the PDF
// golden corpus alone can't catch them — these vectors are the real guard.
// Run: node test/crypto-kat.mjs
import {
  pdfLegacyDigest128, rc4, aesCbcEncryptNoPad, aesCbcDecrypt, aesDecryptPdfData, sha,
} from "../lib/pdf-crypto.js";

let failed = 0;
function hex(u8) {
  return [...u8].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function bytes(h) {
  const o = new Uint8Array(h.length / 2);
  for (let i = 0; i < o.length; i++) o[i] = parseInt(h.slice(i * 2, i * 2 + 2), 16);
  return o;
}
function str(s) {
  return new TextEncoder().encode(s);
}
function eq(name, got, want) {
  if (got === want) {
    console.log(`PASS ${name}`);
  } else {
    failed++;
    console.log(`FAIL ${name}\n  got  ${got}\n  want ${want}`);
  }
}

// ---- RFC 1321 test suite (PDF standard security handler digest) ----
eq("pdfLegacyDigest128('')", hex(pdfLegacyDigest128(str(""))), "d41d8cd98f00b204e9800998ecf8427e");
eq("pdfLegacyDigest128('abc')", hex(pdfLegacyDigest128(str("abc"))), "900150983cd24fb0d6963f7d28e17f72");
eq("pdfLegacyDigest128('message digest')", hex(pdfLegacyDigest128(str("message digest"))), "f96b697d7cb7938d525a2f31aaf161d0");
eq(
  "pdfLegacyDigest128(A-Za-z0-9)",
  hex(pdfLegacyDigest128(str("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"))),
  "d174ab98d277d9f5a5611c2c9f419d9f"
);

// ---- RC4 (Wikipedia / common vectors) ----
eq("rc4 Key/Plaintext", hex(rc4(str("Key"), str("Plaintext"))), "bbf316e8d940af0ad3");
eq("rc4 Wiki/pedia", hex(rc4(str("Wiki"), str("pedia"))), "1021bf0420");
eq("rc4 Secret/Attack at dawn", hex(rc4(str("Secret"), str("Attack at dawn"))), "45a01f645fc35b383552544b9bf5");
// RC4 is symmetric: decrypt round-trips.
eq("rc4 roundtrip", hex(rc4(str("Key"), rc4(str("Key"), str("Plaintext")))), hex(str("Plaintext")));

// ---- AES (FIPS-197 Appendix C single-block, ECB == CBC with zero IV) ----
const ZERO_IV = new Uint8Array(16);
const PT = bytes("00112233445566778899aabbccddeeff");
const K128 = bytes("000102030405060708090a0b0c0d0e0f");
const K192 = bytes("000102030405060708090a0b0c0d0e0f1011121314151617");
const K256 = bytes("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f");
eq("aes-128 enc", hex(aesCbcEncryptNoPad(K128, ZERO_IV, PT)), "69c4e0d86a7b0430d8cdb78070b4c55a");
eq("aes-192 enc", hex(aesCbcEncryptNoPad(K192, ZERO_IV, PT)), "dda97ca4864cdfe06eaf70a0ec0d7191");
eq("aes-256 enc", hex(aesCbcEncryptNoPad(K256, ZERO_IV, PT)), "8ea2b7ca516745bfeafc49904b496089");
eq(
  "aes-128 dec",
  hex(aesCbcDecrypt(K128, ZERO_IV, bytes("69c4e0d86a7b0430d8cdb78070b4c55a"), { removePadding: false })),
  "00112233445566778899aabbccddeeff"
);
eq(
  "aes-256 dec",
  hex(aesCbcDecrypt(K256, ZERO_IV, bytes("8ea2b7ca516745bfeafc49904b496089"), { removePadding: false })),
  "00112233445566778899aabbccddeeff"
);

// ---- AES-CBC multi-block roundtrip + PKCS#7 (mirrors PDF object-data path) ----
{
  const key = bytes("00112233445566778899aabbccddeeff");
  const iv = bytes("0f0e0d0c0b0a09080706050403020100");
  const plain = str("The quick brown fox jumps over 32!"); // 34 bytes -> pads to 48
  const padLen = 16 - (plain.length % 16 || 16) || 16;
  const padded = new Uint8Array(plain.length + padLen);
  padded.set(plain);
  padded.fill(padLen, plain.length);
  const ct = aesCbcEncryptNoPad(key, iv, padded);
  const ivPrefixed = new Uint8Array(16 + ct.length);
  ivPrefixed.set(iv);
  ivPrefixed.set(ct, 16);
  eq("aes-cbc pdf-data roundtrip", hex(aesDecryptPdfData(key, ivPrefixed)), hex(plain));
}

// ---- SHA (WebCrypto passthrough) ----
eq("sha-256('abc')", hex(await sha(256, str("abc"))), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
eq(
  "sha-512('abc')",
  hex(await sha(512, str("abc"))),
  "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f"
);

if (failed) {
  console.log(`\n${failed} KAT(s) failed.`);
  process.exitCode = 1;
} else {
  console.log("\nAll crypto KATs passed.");
}
