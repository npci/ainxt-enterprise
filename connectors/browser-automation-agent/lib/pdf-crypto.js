// lib/pdf-crypto.js — Cryptographic primitives for the PDF standard security
// handler (empty-password decryption), used by lib/pdf.js.
//
// WebCrypto (crypto.subtle) provides SHA-256/384/512 but NOT MD5, RC4, or the
// no-padding AES-CBC modes the PDF R6 key derivation needs, so those are
// implemented here in pure JS. AES is implemented directly (encrypt + decrypt,
// 128/192/256) rather than via crypto.subtle because the PDF spec's R6 hash
// (Algorithm 2.B) uses AES-128-CBC with NO padding, which crypto.subtle cannot
// express. Every primitive here is exercised by test/crypto-kat.mjs against
// published test vectors — crypto bugs surface as plausible garbage, not loud
// failures, so the known-answer tests are the real correctness guard.
//
// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.

// ---------- MD5 (RFC 1321) ----------

const MD5_S = [
  7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
  5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
  4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
  6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
];
const MD5_K = (() => {
  const k = new Uint32Array(64);
  for (let i = 0; i < 64; i++) k[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 2 ** 32) >>> 0;
  return k;
})();

function rotl32(x, c) {
  return ((x << c) | (x >>> (32 - c))) >>> 0;
}

export function md5(bytes) {
  const msgLen = bytes.length;
  // pad: 0x80, then zeros to 56 mod 64, then 64-bit little-endian bit length
  const totalLen = ((msgLen + 8) >> 6) * 64 + 64;
  const buf = new Uint8Array(totalLen);
  buf.set(bytes);
  buf[msgLen] = 0x80;
  const bitLen = msgLen * 8;
  // JS bit ops are 32-bit; write low 32 bits then high 32 bits (byte length
  // never exceeds 2^32 here, so the high word is effectively the >>>32 term).
  const view = new DataView(buf.buffer);
  view.setUint32(totalLen - 8, bitLen >>> 0, true);
  view.setUint32(totalLen - 4, Math.floor(bitLen / 2 ** 32) >>> 0, true);

  let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
  const M = new Uint32Array(16);
  for (let off = 0; off < totalLen; off += 64) {
    for (let i = 0; i < 16; i++) M[i] = view.getUint32(off + i * 4, true);
    let A = a0, B = b0, C = c0, D = d0;
    for (let i = 0; i < 64; i++) {
      let F, g;
      if (i < 16) { F = (B & C) | (~B & D); g = i; }
      else if (i < 32) { F = (D & B) | (~D & C); g = (5 * i + 1) % 16; }
      else if (i < 48) { F = B ^ C ^ D; g = (3 * i + 5) % 16; }
      else { F = C ^ (B | (~D >>> 0)); g = (7 * i) % 16; }
      F = (F + A + MD5_K[i] + M[g]) >>> 0;
      A = D; D = C; C = B;
      B = (B + rotl32(F, MD5_S[i])) >>> 0;
    }
    a0 = (a0 + A) >>> 0; b0 = (b0 + B) >>> 0;
    c0 = (c0 + C) >>> 0; d0 = (d0 + D) >>> 0;
  }
  const out = new Uint8Array(16);
  const ov = new DataView(out.buffer);
  ov.setUint32(0, a0, true); ov.setUint32(4, b0, true);
  ov.setUint32(8, c0, true); ov.setUint32(12, d0, true);
  return out;
}

// ---------- RC4 ----------

export function rc4(key, data) {
  const S = new Uint8Array(256);
  for (let i = 0; i < 256; i++) S[i] = i;
  let j = 0;
  for (let i = 0; i < 256; i++) {
    j = (j + S[i] + key[i % key.length]) & 0xff;
    const t = S[i]; S[i] = S[j]; S[j] = t;
  }
  const out = new Uint8Array(data.length);
  let a = 0, b = 0;
  for (let n = 0; n < data.length; n++) {
    a = (a + 1) & 0xff;
    b = (b + S[a]) & 0xff;
    const t = S[a]; S[a] = S[b]; S[b] = t;
    out[n] = data[n] ^ S[(S[a] + S[b]) & 0xff];
  }
  return out;
}

// ---------- AES (FIPS-197) ----------
// Byte-oriented reference implementation (no T-tables). Fast enough for PDF
// string/stream decryption; correctness is verified by KAT vectors.

const AES_SBOX = new Uint8Array(256);
const AES_INV_SBOX = new Uint8Array(256);
const AES_RCON = new Uint8Array([0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36, 0x6c, 0xd8, 0xab, 0x4d]);

(function initAes() {
  // GF(2^8) multiplicative inverse via log/exp over generator 3, then the
  // affine transform, to build the S-box (and its inverse).
  const exp = new Uint8Array(256);
  const log = new Uint8Array(256);
  let x = 1;
  for (let i = 0; i < 256; i++) {
    exp[i] = x;
    log[x] = i;
    x ^= (x << 1) ^ (x & 0x80 ? 0x11b : 0); // x = x * 3
    x &= 0xff;
  }
  function inv(a) {
    return a === 0 ? 0 : exp[(255 - log[a]) % 255];
  }
  for (let i = 0; i < 256; i++) {
    let s = inv(i);
    let xf = s;
    for (let k = 0; k < 4; k++) {
      xf = (xf << 1) | (xf >>> 7);
      s ^= xf & 0xff;
    }
    s = (s ^ 0x63) & 0xff;
    AES_SBOX[i] = s;
    AES_INV_SBOX[s] = i;
  }
})();

function xtime(a) {
  return ((a << 1) ^ (a & 0x80 ? 0x11b : 0)) & 0xff;
}
function gmul(a, b) {
  let p = 0;
  for (let i = 0; i < 8; i++) {
    if (b & 1) p ^= a;
    const hi = a & 0x80;
    a = (a << 1) & 0xff;
    if (hi) a ^= 0x1b;
    b >>= 1;
  }
  return p & 0xff;
}

function aesKeyExpansion(key) {
  const Nk = key.length / 4; // 4, 6, or 8
  const Nr = Nk + 6;
  const w = new Uint8Array(16 * (Nr + 1));
  w.set(key);
  const totalWords = 4 * (Nr + 1);
  const t = new Uint8Array(4);
  for (let i = Nk; i < totalWords; i++) {
    t[0] = w[(i - 1) * 4]; t[1] = w[(i - 1) * 4 + 1];
    t[2] = w[(i - 1) * 4 + 2]; t[3] = w[(i - 1) * 4 + 3];
    if (i % Nk === 0) {
      const tmp = t[0]; t[0] = t[1]; t[1] = t[2]; t[2] = t[3]; t[3] = tmp; // RotWord
      t[0] = AES_SBOX[t[0]]; t[1] = AES_SBOX[t[1]];
      t[2] = AES_SBOX[t[2]]; t[3] = AES_SBOX[t[3]]; // SubWord
      t[0] ^= AES_RCON[i / Nk - 1];
    } else if (Nk > 6 && i % Nk === 4) {
      t[0] = AES_SBOX[t[0]]; t[1] = AES_SBOX[t[1]];
      t[2] = AES_SBOX[t[2]]; t[3] = AES_SBOX[t[3]];
    }
    for (let k = 0; k < 4; k++) w[i * 4 + k] = w[(i - Nk) * 4 + k] ^ t[k];
  }
  return { w, Nr };
}

function addRoundKey(state, w, round) {
  const off = round * 16;
  for (let i = 0; i < 16; i++) state[i] ^= w[off + i];
}

function aesEncryptBlock(state, w, Nr) {
  addRoundKey(state, w, 0);
  for (let round = 1; round < Nr; round++) {
    for (let i = 0; i < 16; i++) state[i] = AES_SBOX[state[i]]; // SubBytes
    shiftRows(state);
    mixColumns(state);
    addRoundKey(state, w, round);
  }
  for (let i = 0; i < 16; i++) state[i] = AES_SBOX[state[i]];
  shiftRows(state);
  addRoundKey(state, w, Nr);
}

function aesDecryptBlock(state, w, Nr) {
  addRoundKey(state, w, Nr);
  for (let round = Nr - 1; round >= 1; round--) {
    invShiftRows(state);
    for (let i = 0; i < 16; i++) state[i] = AES_INV_SBOX[state[i]];
    addRoundKey(state, w, round);
    invMixColumns(state);
  }
  invShiftRows(state);
  for (let i = 0; i < 16; i++) state[i] = AES_INV_SBOX[state[i]];
  addRoundKey(state, w, 0);
}

// State is column-major (state[r + 4*c]), matching FIPS-197.
function shiftRows(s) {
  let t;
  t = s[1]; s[1] = s[5]; s[5] = s[9]; s[9] = s[13]; s[13] = t;
  t = s[2]; s[2] = s[10]; s[10] = t; t = s[6]; s[6] = s[14]; s[14] = t;
  t = s[15]; s[15] = s[11]; s[11] = s[7]; s[7] = s[3]; s[3] = t;
}
function invShiftRows(s) {
  let t;
  t = s[13]; s[13] = s[9]; s[9] = s[5]; s[5] = s[1]; s[1] = t;
  t = s[2]; s[2] = s[10]; s[10] = t; t = s[6]; s[6] = s[14]; s[14] = t;
  t = s[3]; s[3] = s[7]; s[7] = s[11]; s[11] = s[15]; s[15] = t;
}
function mixColumns(s) {
  for (let c = 0; c < 4; c++) {
    const i = c * 4;
    const a0 = s[i], a1 = s[i + 1], a2 = s[i + 2], a3 = s[i + 3];
    s[i] = xtime(a0) ^ (xtime(a1) ^ a1) ^ a2 ^ a3;
    s[i + 1] = a0 ^ xtime(a1) ^ (xtime(a2) ^ a2) ^ a3;
    s[i + 2] = a0 ^ a1 ^ xtime(a2) ^ (xtime(a3) ^ a3);
    s[i + 3] = (xtime(a0) ^ a0) ^ a1 ^ a2 ^ xtime(a3);
  }
}
function invMixColumns(s) {
  for (let c = 0; c < 4; c++) {
    const i = c * 4;
    const a0 = s[i], a1 = s[i + 1], a2 = s[i + 2], a3 = s[i + 3];
    s[i] = gmul(a0, 14) ^ gmul(a1, 11) ^ gmul(a2, 13) ^ gmul(a3, 9);
    s[i + 1] = gmul(a0, 9) ^ gmul(a1, 14) ^ gmul(a2, 11) ^ gmul(a3, 13);
    s[i + 2] = gmul(a0, 13) ^ gmul(a1, 9) ^ gmul(a2, 14) ^ gmul(a3, 11);
    s[i + 3] = gmul(a0, 11) ^ gmul(a1, 13) ^ gmul(a2, 9) ^ gmul(a3, 14);
  }
}

// AES-CBC encrypt, NO padding (data length must be a multiple of 16). Used only
// by the R6 hardened hash (Algorithm 2.B).
export function aesCbcEncryptNoPad(key, iv, data) {
  const { w, Nr } = aesKeyExpansion(key);
  const out = new Uint8Array(data.length);
  const block = new Uint8Array(16);
  const prev = new Uint8Array(iv);
  for (let off = 0; off < data.length; off += 16) {
    for (let i = 0; i < 16; i++) block[i] = data[off + i] ^ prev[i];
    aesEncryptBlock(block, w, Nr);
    out.set(block, off);
    prev.set(block);
  }
  return out;
}

// AES-CBC decrypt with explicit IV. When removePadding is true, PKCS#7 padding
// is validated and stripped (PDF AESV2/AESV3 object data); when false, raw
// blocks are returned (R6 UE decryption uses a zero IV and no padding).
export function aesCbcDecrypt(key, iv, data, { removePadding = true } = {}) {
  const { w, Nr } = aesKeyExpansion(key);
  const usable = data.length - (data.length % 16);
  const out = new Uint8Array(usable);
  const block = new Uint8Array(16);
  const prev = new Uint8Array(iv);
  for (let off = 0; off < usable; off += 16) {
    for (let i = 0; i < 16; i++) block[i] = data[off + i];
    const cipher = new Uint8Array(block);
    aesDecryptBlock(block, w, Nr);
    for (let i = 0; i < 16; i++) block[i] ^= prev[i];
    out.set(block, off);
    prev.set(cipher);
  }
  if (!removePadding) return out;
  const pad = out[out.length - 1];
  if (pad >= 1 && pad <= 16 && pad <= out.length) return out.subarray(0, out.length - pad);
  return out; // tolerate malformed padding rather than corrupting the tail
}

// Decrypts PDF AES object data: a 16-byte IV prefix followed by PKCS#7-padded
// ciphertext.
export function aesDecryptPdfData(key, ivPrefixedData) {
  if (ivPrefixedData.length < 16) return new Uint8Array(0);
  const iv = ivPrefixedData.subarray(0, 16);
  const ct = ivPrefixedData.subarray(16);
  return aesCbcDecrypt(key, iv, ct, { removePadding: true });
}

// ---------- SHA (via WebCrypto) ----------

const SHA_NAME = { 256: "SHA-256", 384: "SHA-384", 512: "SHA-512" };

export async function sha(bits, bytes) {
  const src = bytes.byteOffset === 0 && bytes.byteLength === bytes.buffer.byteLength
    ? bytes.buffer
    : bytes.slice().buffer;
  const digest = await crypto.subtle.digest(SHA_NAME[bits], src);
  return new Uint8Array(digest);
}
