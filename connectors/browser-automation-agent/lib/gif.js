// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// lib/gif.js — minimal, dependency-free GIF89a encoder for REQ-13 session
// recording. This project has no bundler/package manager, so there is no
// vendored library to lean on — this is a from-scratch, deliberately small
// encoder: a fixed 6x6x6 color cube (216 colors, no dithering) and a uniform
// per-frame delay. It targets a shareable "what happened" debugging replay,
// not a photo-quality export.

// ---------- fixed color-cube palette (216 colors, padded to 256) ----------
const CUBE_LEVELS = [0, 51, 102, 153, 204, 255]; // 6 levels per channel
const PALETTE = [];
for (const r of CUBE_LEVELS) {
  for (const g of CUBE_LEVELS) {
    for (const b of CUBE_LEVELS) PALETTE.push([r, g, b]);
  }
}
while (PALETTE.length < 256) PALETTE.push([0, 0, 0]);

// Direct per-channel quantization to the fixed cube — no nearest-neighbor
// search needed since the cube's levels are evenly spaced.
function colorIndex(r, g, b) {
  const q = (v) => Math.min(5, Math.round(v / 51));
  return q(r) * 36 + q(g) * 6 + q(b);
}

// { data: Uint8ClampedArray (RGBA), width, height } → Uint8Array of palette indices.
function toIndexed({ data, width, height }) {
  const indices = new Uint8Array(width * height);
  for (let i = 0, p = 0; p < width * height; i += 4, p++) {
    indices[p] = colorIndex(data[i], data[i + 1], data[i + 2]);
  }
  return indices;
}

// ---------- LZW (GIF variant): variable code width, clear/EOI codes ----------
// Dictionary keys are numeric — (prefixCode << 8) | nextIndex, where prefixCode
// is the dictionary CODE of the running prefix (root codes 0..255 double as the
// palette indices themselves). The obvious string-concatenation dictionary
// allocates a growing string per pixel, which froze the side panel for seconds
// per megapixel frame; this is the standard integer-keyed formulation. Output
// accumulates in a geometrically-grown Uint8Array for the same reason.
function lzwEncode(indices, minCodeSize) {
  const clearCode = 1 << minCodeSize;
  const eoiCode = clearCode + 1;
  const MAX_CODE_BITS = 12;
  const MAX_DICT = 1 << MAX_CODE_BITS;

  let codeSize, dict, nextCode;
  const resetDict = () => {
    dict = new Map();
    nextCode = eoiCode + 1;
    codeSize = minCodeSize + 1;
  };
  resetDict();

  let out = new Uint8Array(Math.max(1024, indices.length >> 2));
  let outLen = 0;
  const ensure = (n) => {
    if (outLen + n > out.length) {
      const grown = new Uint8Array(Math.max(out.length * 2, outLen + n));
      grown.set(out);
      out = grown;
    }
  };
  let bitBuffer = 0, bitCount = 0;
  const emit = (code) => {
    bitBuffer |= code << bitCount;
    bitCount += codeSize;
    ensure(4);
    while (bitCount >= 8) {
      out[outLen++] = bitBuffer & 0xff;
      bitBuffer >>= 8;
      bitCount -= 8;
    }
  };

  emit(clearCode);
  if (indices.length === 0) {
    emit(eoiCode);
    if (bitCount > 0) { ensure(1); out[outLen++] = bitBuffer & 0xff; }
    return out.subarray(0, outLen);
  }

  let prefix = indices[0]; // a dictionary code (root code = palette index)
  for (let i = 1; i < indices.length; i++) {
    const k = indices[i];
    const key = (prefix << 8) | k;
    const found = dict.get(key);
    if (found !== undefined) {
      prefix = found;
      continue;
    }
    emit(prefix);
    if (nextCode < MAX_DICT) {
      dict.set(key, nextCode++);
      if (nextCode > (1 << codeSize) && codeSize < MAX_CODE_BITS) codeSize++;
    } else {
      emit(clearCode);
      resetDict();
    }
    prefix = k;
  }
  emit(prefix);
  emit(eoiCode);
  if (bitCount > 0) { ensure(1); out[outLen++] = bitBuffer & 0xff; }
  return out.subarray(0, outLen);
}

// ---------- GIF89a container ----------
function pushHeader(bytes, width, height) {
  bytes.push(0x47, 0x49, 0x46, 0x38, 0x39, 0x61); // "GIF89a"
  bytes.push(width & 0xff, (width >> 8) & 0xff);
  bytes.push(height & 0xff, (height >> 8) & 0xff);
  // Packed field: GCT present, 8-bit color resolution, not sorted, GCT size 2^(7+1)=256.
  bytes.push(0xf7);
  bytes.push(0x00); // background color index
  bytes.push(0x00); // pixel aspect ratio
}

function pushGlobalColorTable(bytes) {
  for (const [r, g, b] of PALETTE) bytes.push(r, g, b);
}

function pushLoopExtension(bytes) {
  bytes.push(0x21, 0xff, 0x0b);
  for (const ch of "NETSCAPE2.0") bytes.push(ch.charCodeAt(0));
  bytes.push(0x03, 0x01, 0x00, 0x00, 0x00); // loop forever
}

function pushGraphicControl(bytes, delayCsec) {
  bytes.push(0x21, 0xf9, 0x04);
  bytes.push(0x04); // disposal method 1 (do not dispose — each frame is a full redraw anyway)
  bytes.push(delayCsec & 0xff, (delayCsec >> 8) & 0xff);
  bytes.push(0x00); // transparent color index (unused)
  bytes.push(0x00); // block terminator
}

function pushImageDescriptor(bytes, width, height) {
  bytes.push(0x2c);
  bytes.push(0, 0, 0, 0); // left, top
  bytes.push(width & 0xff, (width >> 8) & 0xff);
  bytes.push(height & 0xff, (height >> 8) & 0xff);
  bytes.push(0x00); // no local color table, not interlaced
}

// Wrap LZW output into GIF's ≤255-byte sub-blocks in one preallocated buffer.
function subBlock(lzwBytes) {
  const nBlocks = Math.ceil(lzwBytes.length / 255);
  const out = new Uint8Array(lzwBytes.length + nBlocks + 1);
  let o = 0;
  for (let i = 0; i < lzwBytes.length; i += 255) {
    const len = Math.min(255, lzwBytes.length - i);
    out[o++] = len;
    out.set(lzwBytes.subarray(i, i + len), o);
    o += len;
  }
  out[o] = 0x00; // block terminator
  return out;
}

// frames: [{ data, width, height }] (all frames must share width/height —
// callers should draw every frame onto a same-size canvas first).
// Returns a Blob (type: image/gif). Assembled as a list of Uint8Array parts
// (Blob concatenates them) so multi-MB image data never round-trips through a
// per-byte JS array.
export function encodeGif(frames, { delayMs = 800 } = {}) {
  if (!frames?.length) throw new Error("encodeGif: no frames");
  const { width, height } = frames[0];
  const parts = [];
  const head = [];
  pushHeader(head, width, height);
  pushGlobalColorTable(head);
  pushLoopExtension(head);
  parts.push(new Uint8Array(head));
  const delayCsec = Math.max(2, Math.round(delayMs / 10));
  const minCodeSize = 8; // full 256-entry palette
  for (const frame of frames) {
    const meta = [];
    pushGraphicControl(meta, delayCsec);
    pushImageDescriptor(meta, width, height);
    meta.push(minCodeSize);
    parts.push(new Uint8Array(meta));
    parts.push(subBlock(lzwEncode(toIndexed(frame), minCodeSize)));
  }
  parts.push(new Uint8Array([0x3b])); // trailer
  return new Blob(parts, { type: "image/gif" });
}
