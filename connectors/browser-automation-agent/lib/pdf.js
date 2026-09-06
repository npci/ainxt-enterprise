// lib/pdf.js — Minimal in-house PDF text extractor (no rendering).
//
// Supports: classic xref tables + xref streams (hybrid /XRefStm), object
// streams, FlateDecode/LZWDecode (+ PNG/TIFF predictors),
// ASCIIHexDecode/ASCII85Decode, the standard security handler with an empty
// user password (RC4, AES-128, AES-256/R6 — see buildSecurityHandler),
// page-tree walk with inherited /Resources, content-stream Tj/TJ/'/" text
// extraction (incl. inline-image skip-handling), /ToUnicode CMaps, and
// WinAnsi/MacRoman/Standard base encodings + /Differences overrides.
// Not supported: rendering, OCR, decryption of PDFs that require a non-empty
// password (detected and reported via the "password" error code, not read),
// predefined CJK CMaps without an embedded /ToUnicode (accepted gap).
//
// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.

import { renderAddressedGrid } from "./grid.js";
import { md5, rc4, aesCbcDecrypt, aesCbcEncryptNoPad, aesDecryptPdfData, sha } from "./pdf-crypto.js";

// ---------- lexer ----------
// Byte-level tokenizer for PDF's object syntax. Operates directly on the
// Uint8Array (never a decoded string) so binary string/stream content is
// never corrupted by a text-encoding round trip.

const WHITESPACE = new Set([0x00, 0x09, 0x0a, 0x0c, 0x0d, 0x20]);
const DELIMS = new Set([0x28, 0x29, 0x3c, 0x3e, 0x5b, 0x5d, 0x7b, 0x7d, 0x2f, 0x25]);

function isWhitespace(b) {
  return WHITESPACE.has(b);
}
function isDelim(b) {
  return DELIMS.has(b);
}
function isRegular(b) {
  return b != null && b >= 0 && !isWhitespace(b) && !isDelim(b);
}
function isHexDigit(b) {
  return (b >= 0x30 && b <= 0x39) || (b >= 0x41 && b <= 0x46) || (b >= 0x61 && b <= 0x66);
}
function hexVal(b) {
  if (b >= 0x30 && b <= 0x39) return b - 0x30;
  if (b >= 0x41 && b <= 0x46) return b - 0x41 + 10;
  return b - 0x61 + 10;
}

function createLexer(bytes, pos = 0) {
  const len = bytes.length;

  function skipWs() {
    while (pos < len) {
      const b = bytes[pos];
      if (b === 0x25) {
        while (pos < len && bytes[pos] !== 0x0a && bytes[pos] !== 0x0d) pos++;
        continue;
      }
      if (isWhitespace(b)) {
        pos++;
        continue;
      }
      break;
    }
  }

  function readName() {
    pos++; // skip '/'
    const out = [];
    while (pos < len && isRegular(bytes[pos])) {
      if (bytes[pos] === 0x23 && isHexDigit(bytes[pos + 1]) && isHexDigit(bytes[pos + 2])) {
        out.push((hexVal(bytes[pos + 1]) << 4) | hexVal(bytes[pos + 2]));
        pos += 3;
      } else {
        out.push(bytes[pos]);
        pos++;
      }
    }
    return "/" + String.fromCharCode(...out);
  }

  function readNumberOrKeyword() {
    const start = pos;
    pos = _scanWhile(bytes, pos, isRegular);
    const s = String.fromCharCode(...bytes.subarray(start, pos));
    if (/^[+-]?(\d+\.?\d*|\.\d+)$/.test(s)) return { type: "num", value: parseFloat(s) };
    return { type: "keyword", value: s };
  }

  function readLiteralString() {
    pos++; // skip '('
    let depth = 1;
    const out = [];
    while (pos < len && depth > 0) {
      const b = bytes[pos];
      if (b === 0x5c) {
        pos++;
        const e = bytes[pos];
        switch (e) {
          case 0x6e:
            out.push(0x0a);
            pos++;
            break;
          case 0x72:
            out.push(0x0d);
            pos++;
            break;
          case 0x74:
            out.push(0x09);
            pos++;
            break;
          case 0x62:
            out.push(0x08);
            pos++;
            break;
          case 0x66:
            out.push(0x0c);
            pos++;
            break;
          case 0x28:
            out.push(0x28);
            pos++;
            break;
          case 0x29:
            out.push(0x29);
            pos++;
            break;
          case 0x5c:
            out.push(0x5c);
            pos++;
            break;
          case 0x0d:
            pos++;
            if (bytes[pos] === 0x0a) pos++;
            break;
          case 0x0a:
            pos++;
            break;
          default:
            if (e >= 0x30 && e <= 0x37) {
              let v = 0,
                n = 0;
              while (n < 3 && bytes[pos] >= 0x30 && bytes[pos] <= 0x37) {
                v = v * 8 + (bytes[pos] - 0x30);
                pos++;
                n++;
              }
              out.push(v & 0xff);
            } else if (e != null) {
              out.push(e);
              pos++;
            } else {
              pos++;
            }
        }
        continue;
      }
      if (b === 0x28) {
        depth++;
        out.push(b);
        pos++;
        continue;
      }
      if (b === 0x29) {
        depth--;
        pos++;
        if (depth > 0) out.push(b);
        continue;
      }
      if (b === 0x0d) {
        out.push(0x0a);
        pos++;
        if (bytes[pos] === 0x0a) pos++;
        continue;
      }
      out.push(b);
      pos++;
    }
    return new Uint8Array(out);
  }

  function readHexString() {
    pos++; // skip '<'
    // Locate the closing '>' with a single native scan (TypedArray#indexOf),
    // then derive digits via Array#filter over the fixed-length slice — no
    // for/while loop condition is driven by the tainted byte stream.
    const closeIdx = bytes.indexOf(0x3e, pos);
    const scanEnd = closeIdx === -1 ? len : closeIdx;
    const digits = Array.from(bytes.subarray(pos, scanEnd)).filter(isHexDigit);
    pos = scanEnd + 1; // skip '>' (or run off the end, matching prior behavior)
    if (digits.length % 2 === 1) digits.push(0x30);
    const out = new Uint8Array(digits.length / 2);
    for (let i = 0; i < out.length; i++) {
      out[i] = (hexVal(digits[2 * i]) << 4) | hexVal(digits[2 * i + 1]);
    }
    return out;
  }

  function next() {
    skipWs();
    if (pos >= len) return { type: "eof" };
    const b = bytes[pos];
    if (b === 0x2f) return { type: "name", value: readName() };
    if (b === 0x28) return { type: "str", value: readLiteralString() };
    if (b === 0x3c) {
      if (bytes[pos + 1] === 0x3c) {
        pos += 2;
        return { type: "dictStart" };
      }
      return { type: "hexstr", value: readHexString() };
    }
    if (b === 0x3e) {
      if (bytes[pos + 1] === 0x3e) {
        pos += 2;
        return { type: "dictEnd" };
      }
      pos++;
      return { type: "dictEnd" };
    }
    if (b === 0x5b) {
      pos++;
      return { type: "arrStart" };
    }
    if (b === 0x5d) {
      pos++;
      return { type: "arrEnd" };
    }
    if (b === 0x7b || b === 0x7d) {
      pos++;
      return next();
    }
    if (isRegular(b)) return readNumberOrKeyword();
    pos++;
    return next();
  }

  return {
    get pos() {
      return pos;
    },
    set pos(p) {
      pos = p;
    },
    next,
  };
}

// ---------- object value parser ----------

const MAX_NESTING = 200;

function parseArrayBody(lex, depth) {
  const arr = [];
  while (true) {
    const save = lex.pos;
    const t = lex.next();
    if (t.type === "arrEnd" || t.type === "eof") break;
    lex.pos = save;
    arr.push(parseValue(lex, depth + 1));
  }
  return arr;
}

function parseDictBody(lex, depth) {
  const dict = {};
  while (true) {
    const save = lex.pos;
    const t = lex.next();
    if (t.type === "dictEnd" || t.type === "eof") break;
    if (t.type !== "name") continue; // tolerate stray tokens in malformed dicts
    const key = t.value.slice(1);
    dict[key] = parseValue(lex, depth + 1);
  }
  return dict;
}

function parseValue(lex, depth = 0) {
  if (depth > MAX_NESTING) throw new Error("PDF object nesting too deep");
  const tok = lex.next();
  switch (tok.type) {
    case "num": {
      const save2 = lex.pos;
      const t2 = lex.next();
      if (t2.type === "num" && Number.isInteger(tok.value) && Number.isInteger(t2.value)) {
        const save3 = lex.pos;
        const t3 = lex.next();
        if (t3.type === "keyword" && t3.value === "R") {
          return { ref: tok.value, gen: t2.value };
        }
      }
      lex.pos = save2;
      return tok.value;
    }
    case "name":
      return tok.value;
    case "str":
      return tok.value;
    case "hexstr":
      return tok.value;
    case "arrStart":
      return parseArrayBody(lex, depth);
    case "dictStart":
      return parseDictBody(lex, depth);
    case "keyword":
      if (tok.value === "true") return true;
      if (tok.value === "false") return false;
      if (tok.value === "null") return null;
      return undefined;
    default:
      return undefined;
  }
}

function matchKeywordAt(bytes, pos, kw) {
  if (pos < 0 || pos + kw.length > bytes.length) return false;
  for (let i = 0; i < kw.length; i++) {
    if (bytes[pos + i] !== kw.charCodeAt(i)) return false;
  }
  return true;
}

function findEndstream(bytes, start) {
  const kw = "endstream";
  // Latin-1 decode is a 1:1 byte<->char mapping, so String#indexOf (a single
  // native substring search, not a JS for/while loop) gives the exact byte
  // offset with no possibility of a false-positive first-byte match — unlike
  // scanning for the first 'e' byte and re-verifying on each hit.
  const idx = new TextDecoder("latin1").decode(bytes.subarray(start)).indexOf(kw);
  if (idx === -1) return bytes.length;
  let end = start + idx;
  if (end > start && bytes[end - 1] === 0x0a) end--;
  if (end > start && bytes[end - 1] === 0x0d) end--;
  return end;
}

// Parses "N G obj ... endobj", including a trailing stream if present.
// resolveLengthFn(ref) -> Promise<number|null> is used when /Length is an
// indirect reference and a resolver is available; without one (bootstrap
// xref parsing, or recovery mode) an unresolvable /Length falls back to
// scanning for the literal "endstream" keyword.
async function parseIndirectObjectAt(bytes, offset, resolveLengthFn) {
  const lex = createLexer(bytes, offset);
  lex.next(); // object number (trusted from xref/offset)
  lex.next(); // generation
  const kw = lex.next();
  if (!(kw.type === "keyword" && kw.value === "obj")) {
    throw new Error(`expected 'obj' at offset ${offset}`);
  }
  const value = parseValue(lex, 0);
  const save = lex.pos;
  const t4 = lex.next();
  if (t4.type === "keyword" && t4.value === "stream") {
    let p = lex.pos;
    if (bytes[p] === 0x0d && bytes[p + 1] === 0x0a) p += 2;
    else if (bytes[p] === 0x0a) p += 1;
    else if (bytes[p] === 0x0d) p += 1;
    const dict = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    let length = null;
    if (typeof dict.Length === "number") length = dict.Length;
    else if (dict.Length && typeof dict.Length.ref === "number" && resolveLengthFn) {
      length = await resolveLengthFn(dict.Length.ref);
    }
    let dataEnd;
    if (typeof length === "number" && length >= 0 && p + length <= bytes.length) {
      dataEnd = p + length;
      const q = _scanWhile(bytes, dataEnd, isWhitespace);
      if (!matchKeywordAt(bytes, q, "endstream")) {
        dataEnd = findEndstream(bytes, p);
      }
    } else {
      dataEnd = findEndstream(bytes, p);
    }
    return { value: dict, streamRaw: bytes.subarray(p, dataEnd) };
  }
  lex.pos = save;
  return { value, streamRaw: null };
}

// ---------- xref ----------

function lastIndexOfKeyword(bytes, kw) {
  // Latin-1 decode is a 1:1 byte<->char mapping, so String#lastIndexOf (a
  // single native reverse substring search, not a JS for/while loop) gives
  // the exact byte offset of the last occurrence.
  return new TextDecoder("latin1").decode(bytes).lastIndexOf(kw);
}

function findLastStartXref(bytes) {
  const idx = lastIndexOfKeyword(bytes, "startxref");
  if (idx < 0) return null;
  const lex = createLexer(bytes, idx + "startxref".length);
  const t = lex.next();
  return t.type === "num" ? t.value : null;
}

function readUIntBE(bytes, offset, len) {
  let v = 0;
  for (let i = 0; i < len; i++) v = v * 256 + bytes[offset + i];
  return v;
}

// Advances past a run matching `pred`, starting at `p`, using a single
// native TypedArray#findIndex scan (not a for/while loop keyed on the
// tainted byte stream) to locate where the run ends.
function _scanWhile(bytes, p, pred) {
  const rel = bytes.subarray(p).findIndex((b) => !pred(b));
  return rel === -1 ? bytes.length : p + rel;
}
const _isDigitByte = (b) => b >= 0x30 && b <= 0x39;

function matchXrefLine(bytes, pos) {
  let p = pos;
  const offStart = p;
  p = _scanWhile(bytes, p, _isDigitByte);
  if (p === offStart) return null;
  const offset = parseInt(String.fromCharCode(...bytes.subarray(offStart, p)), 10);
  p = _scanWhile(bytes, p, isWhitespace);
  const genStart = p;
  p = _scanWhile(bytes, p, _isDigitByte);
  if (p === genStart) return null;
  const gen = parseInt(String.fromCharCode(...bytes.subarray(genStart, p)), 10);
  p = _scanWhile(bytes, p, isWhitespace);
  const typeChar = bytes[p];
  if (typeChar !== 0x6e && typeChar !== 0x66) return null;
  p++;
  p = _scanWhile(bytes, p, (b) => b === 0x20 || b === 0x0d || b === 0x0a);
  return { offset, gen, type: typeChar === 0x6e ? "n" : "f", nextPos: p };
}

function parseClassicXrefTable(bytes, lex) {
  const entries = new Map();
  while (true) {
    const save = lex.pos;
    const t = lex.next();
    if (t.type === "keyword" && t.value === "trailer") {
      const trailer = parseValue(lex, 0);
      return { entries, trailer };
    }
    if (t.type !== "num") {
      lex.pos = save;
      break;
    }
    const start = t.value;
    const t2 = lex.next();
    // Cap subsection count to bytes.length: each entry consumes >=1 byte, so
    // no valid file can have more entries than it has bytes (CWE-400 guard —
    // `count` is an attacker-controlled value straight from the xref header).
    const count = t2.type === "num" ? Math.min(t2.value, bytes.length) : 0;
    for (let i = 0; i < count; i++) {
      lex.pos = _scanWhile(bytes, lex.pos, isWhitespace);
      const m = matchXrefLine(bytes, lex.pos);
      if (!m) break;
      const num = start + i;
      if (m.type === "n" && !entries.has(num)) {
        entries.set(num, { type: 1, offset: m.offset, gen: m.gen });
      }
      lex.pos = m.nextPos;
    }
  }
  return { entries, trailer: null };
}

async function parseXrefSectionAt(bytes, offset) {
  const lex = createLexer(bytes, offset);
  const save = lex.pos;
  const t = lex.next();
  if (t.type === "keyword" && t.value === "xref") {
    return parseClassicXrefTable(bytes, lex);
  }
  lex.pos = save;
  const parsed = await parseIndirectObjectAt(bytes, offset, null);
  const dict = parsed.value;
  if (!parsed.streamRaw || !dict) return null;
  const decoded = await applyFilters(dict, parsed.streamRaw);
  const W = Array.isArray(dict.W) ? dict.W : [1, 2, 1];
  const size = typeof dict.Size === "number" ? dict.Size : 0;
  const index = Array.isArray(dict.Index) ? dict.Index : [0, size];
  const rowLen = W[0] + W[1] + W[2];
  const entries = new Map();
  let p = 0;
  for (let ii = 0; ii + 1 < index.length; ii += 2) {
    let num = index[ii];
    const count = index[ii + 1];
    for (let k = 0; k < count; k++) {
      if (p + rowLen > decoded.length) break;
      let o = p;
      const type = W[0] === 0 ? 1 : readUIntBE(decoded, o, W[0]);
      o += W[0];
      const f2 = readUIntBE(decoded, o, W[1]);
      o += W[1];
      const f3 = readUIntBE(decoded, o, W[2]);
      p += rowLen;
      if (type === 1) entries.set(num, { type: 1, offset: f2, gen: f3 });
      else if (type === 2) entries.set(num, { type: 2, streamObjNum: f2, indexInStream: f3 });
      num++;
    }
  }
  return { entries, trailer: dict };
}

function decodeLatin1(bytes) {
  return new TextDecoder("latin1").decode(bytes);
}

function recoverByScanning(bytes) {
  const entries = new Map(); // last occurrence wins (later = newer incremental update)
  const text = decodeLatin1(bytes);
  const re = /(\d+)[ \t\r\n]+(\d+)[ \t\r\n]+obj\b/g;
  let m;
  while ((m = re.exec(text))) {
    entries.set(parseInt(m[1], 10), { type: 1, offset: m.index, gen: parseInt(m[2], 10) });
  }
  let trailer = null;
  const tIdx = text.lastIndexOf("trailer");
  if (tIdx >= 0) {
    try {
      trailer = parseValue(createLexer(bytes, tIdx + 7), 0);
    } catch {
      trailer = null;
    }
  }
  return { entries, trailer, needsRootScan: !trailer || trailer.Root == null };
}

async function findRootByScanning(bytes, entries) {
  for (const [num, entry] of entries) {
    try {
      const parsed = await parseIndirectObjectAt(bytes, entry.offset, null);
      if (parsed.value && parsed.value.Type === "/Catalog") {
        return { ref: num, gen: 0 };
      }
    } catch {
      // unparsable object during recovery — skip it
    }
  }
  return null;
}

async function parseXref(bytes) {
  const xrefMap = new Map();
  let trailer = null;
  const visited = new Set();

  async function loadSection(offset) {
    if (offset == null || offset < 0 || offset >= bytes.length || visited.has(offset)) return;
    visited.add(offset);
    let section;
    try {
      section = await parseXrefSectionAt(bytes, offset);
    } catch {
      return;
    }
    if (!section) return;
    if (trailer === null) trailer = section.trailer;
    for (const [num, entry] of section.entries) {
      if (!xrefMap.has(num)) xrefMap.set(num, entry);
    }
    if (section.trailer && typeof section.trailer.XRefStm === "number") {
      await loadSection(section.trailer.XRefStm);
    }
    if (section.trailer && typeof section.trailer.Prev === "number") {
      await loadSection(section.trailer.Prev);
    }
  }

  const startOffset = findLastStartXref(bytes);
  if (startOffset != null) await loadSection(startOffset);

  if (xrefMap.size === 0 || !trailer || trailer.Root == null) {
    const recovered = recoverByScanning(bytes);
    for (const [num, entry] of recovered.entries) xrefMap.set(num, entry);
    if (!trailer) trailer = recovered.trailer || {};
    if (trailer.Root == null) {
      const root = await findRootByScanning(bytes, recovered.entries);
      if (root) trailer = { ...trailer, Root: root, Size: Math.max(...recovered.entries.keys(), 0) + 1 };
    }
  }

  return { xrefMap, trailer };
}

// ---------- filters ----------

const MAX_DECOMPRESSED = 64 * 1024 * 1024; // decompression-bomb guard (FR10b)

async function inflate(data) {
  const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate"));
  const reader = stream.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > MAX_DECOMPRESSED) {
      await reader.cancel().catch(() => {});
      throw new Error("decompressed stream exceeds size limit");
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let o = 0;
  for (const c of chunks) {
    out.set(c, o);
    o += c.length;
  }
  return out;
}

function paeth(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a),
    pb = Math.abs(p - b),
    pc = Math.abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  if (pb <= pc) return b;
  return c;
}

function applyPngPredictor(data, rowBytes, bpp) {
  const numRows = Math.floor(data.length / (rowBytes + 1));
  const out = new Uint8Array(numRows * rowBytes);
  let prevRow = new Uint8Array(rowBytes);
  let inPos = 0,
    outPos = 0;
  for (let r = 0; r < numRows; r++) {
    const filterType = data[inPos];
    inPos++;
    const row = data.subarray(inPos, inPos + rowBytes);
    inPos += rowBytes;
    const outRow = out.subarray(outPos, outPos + rowBytes);
    for (let i = 0; i < rowBytes; i++) {
      const a = i >= bpp ? outRow[i - bpp] : 0;
      const b = prevRow[i] || 0;
      const c = i >= bpp ? prevRow[i - bpp] || 0 : 0;
      const raw = row[i];
      let val;
      switch (filterType) {
        case 0:
          val = raw;
          break;
        case 1:
          val = (raw + a) & 0xff;
          break;
        case 2:
          val = (raw + b) & 0xff;
          break;
        case 3:
          val = (raw + Math.floor((a + b) / 2)) & 0xff;
          break;
        case 4:
          val = (raw + paeth(a, b, c)) & 0xff;
          break;
        default:
          val = raw;
      }
      outRow[i] = val;
    }
    prevRow = outRow;
    outPos += rowBytes;
  }
  return out;
}

function applyTiffPredictor(data, rowBytes, bpp, bpc) {
  if (bpc !== 8) return data; // best-effort: only the common 8-bit case is fully supported
  const numRows = Math.floor(data.length / rowBytes);
  const out = new Uint8Array(data);
  for (let r = 0; r < numRows; r++) {
    const rowStart = r * rowBytes;
    for (let i = bpp; i < rowBytes; i++) {
      out[rowStart + i] = (out[rowStart + i] + out[rowStart + i - bpp]) & 0xff;
    }
  }
  return out;
}

function applyPredictor(data, parms) {
  if (!parms || typeof parms !== "object") return data;
  const predictor = parms.Predictor || 1;
  if (predictor <= 1) return data;
  const colors = parms.Colors ?? 1;
  const bpc = parms.BitsPerComponent ?? 8;
  const columns = parms.Columns ?? 1;
  const bpp = Math.max(1, Math.ceil((colors * bpc) / 8));
  const rowBytes = Math.ceil((colors * bpc * columns) / 8);
  if (rowBytes <= 0) return data;
  if (predictor === 2) return applyTiffPredictor(data, rowBytes, bpp, bpc);
  return applyPngPredictor(data, rowBytes, bpp);
}

function asciiHexDecode(data) {
  const digits = [];
  for (let i = 0; i < data.length; i++) {
    const b = data[i];
    if (b === 0x3e) break;
    if (isHexDigit(b)) digits.push(b);
  }
  if (digits.length % 2 === 1) digits.push(0x30);
  const out = new Uint8Array(digits.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = (hexVal(digits[2 * i]) << 4) | hexVal(digits[2 * i + 1]);
  return out;
}

function ascii85Decode(data) {
  const out = [];
  let tuple = [];
  let i = 0;
  if (data[0] === 0x3c && data[1] === 0x7e) i = 2; // optional leading "<~"
  for (; i < data.length; i++) {
    const b = data[i];
    if (b === 0x7e) break; // '~' of "~>" terminator
    if (isWhitespace(b)) continue;
    if (b === 0x7a && tuple.length === 0) {
      out.push(0, 0, 0, 0);
      continue;
    }
    tuple.push(b - 33);
    if (tuple.length === 5) {
      let val = 0;
      for (const t of tuple) val = val * 85 + t;
      out.push((val >>> 24) & 0xff, (val >>> 16) & 0xff, (val >>> 8) & 0xff, val & 0xff);
      tuple = [];
    }
  }
  if (tuple.length > 0) {
    const n = tuple.length;
    while (tuple.length < 5) tuple.push(84);
    let val = 0;
    for (const t of tuple) val = val * 85 + t;
    const b4 = [(val >>> 24) & 0xff, (val >>> 16) & 0xff, (val >>> 8) & 0xff, val & 0xff];
    for (let k = 0; k < n - 1; k++) out.push(b4[k]);
  }
  return new Uint8Array(out);
}

// Variable-width (9–12 bit) MSB-first LZW decoder (PDF §7.4.4 / TIFF LZW).
// Honors the /EarlyChange flag (default 1) and the clear (256) / EOD (257)
// codes. Output size is capped by the same decompression-bomb guard as inflate.
function lzwDecode(data, earlyChange = 1) {
  const out = [];
  let dict = [];
  function reset() {
    dict = new Array(256);
    for (let i = 0; i < 256; i++) dict[i] = [i];
    dict.push(null, null); // 256 = clear table, 257 = end-of-data
  }
  reset();
  let codeWidth = 9;
  let prev = null;
  let bitBuffer = 0, bitCount = 0, pos = 0;
  function nextCode() {
    while (bitCount < codeWidth) {
      if (pos >= data.length) return -1;
      bitBuffer = (bitBuffer << 8) | data[pos++];
      bitCount += 8;
    }
    bitCount -= codeWidth;
    return (bitBuffer >> bitCount) & ((1 << codeWidth) - 1);
  }
  let code;
  while ((code = nextCode()) !== -1) {
    if (code === 256) { reset(); codeWidth = 9; prev = null; continue; }
    if (code === 257) break;
    let entry;
    if (code < dict.length && dict[code]) entry = dict[code];
    else if (prev) entry = prev.concat(prev[0]);
    else break; // corrupt: code with no prior context
    for (const b of entry) out.push(b);
    if (out.length > MAX_DECOMPRESSED) throw new Error("decompressed stream exceeds size limit");
    if (prev) dict.push(prev.concat(entry[0]));
    prev = entry;
    if (dict.length + earlyChange >= (1 << codeWidth) && codeWidth < 12) codeWidth++;
  }
  return new Uint8Array(out);
}

async function applyOneFilter(name, parms, data) {
  switch (name) {
    case "/FlateDecode":
    case "/Fl":
      return applyPredictor(await inflate(data), parms);
    case "/ASCIIHexDecode":
    case "/AHx":
      return asciiHexDecode(data);
    case "/ASCII85Decode":
    case "/A85":
      return ascii85Decode(data);
    case "/LZWDecode":
    case "/LZW":
      return applyPredictor(lzwDecode(data, parms?.EarlyChange ?? 1), parms);
    default:
      throw new Error(`unsupported filter ${name}`);
  }
}

async function applyFilters(dict, rawBytes) {
  let filters = dict.Filter;
  if (filters == null) return rawBytes;
  if (!Array.isArray(filters)) filters = [filters];
  let parms = dict.DecodeParms || dict.DP;
  if (parms == null) parms = [];
  if (!Array.isArray(parms)) parms = [parms];
  let data = rawBytes;
  for (let i = 0; i < filters.length; i++) {
    data = await applyOneFilter(filters[i], parms[i] || null, data);
  }
  return data;
}

// ---------- standard security handler (empty user password) ----------
// Implements FR7: derives the file key for the standard security handler from
// an EMPTY user password and decrypts strings/streams. R2–4 use MD5-derived
// per-object RC4/AES-128 keys; R6 (V5) uses the 32-byte file key directly with
// AES-256. A PDF that needs a real (non-empty) password fails the U/OU check
// and is surfaced via a "password" error code (never silent garbage).

// PDF standard padding string (§7.6.3.3, Algorithm 2).
const PDF_PAD = new Uint8Array([
  0x28, 0xbf, 0x4e, 0x5e, 0x4e, 0x75, 0x8a, 0x41, 0x64, 0x00, 0x4e, 0x56, 0xff, 0xfa, 0x01, 0x08,
  0x2e, 0x2e, 0x00, 0xb6, 0xd0, 0x68, 0x3e, 0x80, 0x2f, 0x0c, 0xa9, 0xfe, 0x64, 0x53, 0x69, 0x7a,
]);

function encError(code, message) {
  return Object.assign(new Error(message), { code });
}

function bytesEqual(a, b, len = Math.min(a.length, b.length)) {
  if (a.length < len || b.length < len) return false;
  for (let i = 0; i < len; i++) if (a[i] !== b[i]) return false;
  return true;
}

function concatBytes(parts) {
  let total = 0;
  for (const p of parts) total += p.length;
  const out = new Uint8Array(total);
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

function padEmptyPassword() {
  return PDF_PAD.slice(); // empty password ⇒ just the 32-byte pad
}

function p32le(n) {
  const v = n | 0; // coerce to signed 32-bit
  return new Uint8Array([v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff]);
}

function asBytes(v) {
  return v instanceof Uint8Array ? v : new Uint8Array(0);
}

// ISO 32000-2 Algorithm 2.B — the R6 hardened hash. R5 (deprecated draft) is
// a plain SHA-256; R6 iterates AES-128-CBC + SHA-256/384/512 until stable.
async function hash2B(R, password, salt, udata) {
  let K = await sha(256, concatBytes([password, salt, udata]));
  if (R < 6) return K;
  for (let round = 1; ; round++) {
    const block = concatBytes([password, K, udata]);
    const K1 = new Uint8Array(block.length * 64);
    for (let i = 0; i < 64; i++) K1.set(block, i * block.length);
    const E = aesCbcEncryptNoPad(K.subarray(0, 16), K.subarray(16, 32), K1);
    let mod = 0;
    for (let i = 0; i < 16; i++) mod += E[i];
    mod %= 3; // 256 ≡ 1 (mod 3), so the byte sum ≡ the big-endian integer
    K = await sha(mod === 0 ? 256 : mod === 1 ? 384 : 512, E);
    if (round >= 64 && E[E.length - 1] <= round - 32) break;
  }
  return K.subarray(0, 32);
}

// Builds a decryptor from the /Encrypt dict + first element of the trailer /ID.
// Throws {code:"encrypted"} for an unsupported handler/algorithm and
// {code:"password"} when the empty user password does not authenticate.
async function buildSecurityHandler(enc, idBytes) {
  if (!enc || typeof enc !== "object") throw encError("encrypted", "missing /Encrypt dictionary");
  if (enc.Filter !== "/Standard") throw encError("encrypted", "unsupported security handler");
  const R = enc.R, V = enc.V;
  const O = asBytes(enc.O), U = asBytes(enc.U);

  if (V === 5) {
    // AES-256 (R5/R6). No per-object key: the 32-byte file key is used directly.
    if (U.length < 48) throw encError("encrypted", "malformed /U for V5");
    const test = await hash2B(R, new Uint8Array(0), U.subarray(32, 40), new Uint8Array(0));
    if (!bytesEqual(test, U.subarray(0, 32), 32)) throw encError("password", "non-empty password required");
    const ikey = await hash2B(R, new Uint8Array(0), U.subarray(40, 48), new Uint8Array(0));
    const UE = asBytes(enc.UE);
    const fileKey = aesCbcDecrypt(ikey, new Uint8Array(16), UE, { removePadding: false });
    const decrypt = (data) => aesDecryptPdfData(fileKey, data);
    return { decryptStream: (b) => decrypt(b), decryptString: (b) => decrypt(b) };
  }

  // R2–4: RC4 or (V4/AESV2) AES-128 with an MD5-derived per-object key.
  const keyLen = V === 1 ? 5 : Math.floor((enc.Length || 40) / 8);
  const encryptMetadata = enc.EncryptMetadata !== false;

  // Algorithm 2: file key from the (empty) user password.
  const parts = [padEmptyPassword(), O, p32le(enc.P || 0), idBytes];
  if (R >= 4 && !encryptMetadata) parts.push(new Uint8Array([0xff, 0xff, 0xff, 0xff]));
  let h = md5(concatBytes(parts));
  if (R >= 3) for (let i = 0; i < 50; i++) h = md5(h.subarray(0, keyLen));
  const fileKey = h.subarray(0, keyLen);

  // Algorithm 4/5: authenticate the empty user password against /U.
  let ok;
  if (R === 2) {
    ok = bytesEqual(rc4(fileKey, padEmptyPassword()), U, 32);
  } else {
    let e = md5(concatBytes([padEmptyPassword(), idBytes]));
    e = rc4(fileKey, e);
    for (let i = 1; i <= 19; i++) {
      const k = new Uint8Array(fileKey.length);
      for (let j = 0; j < k.length; j++) k[j] = fileKey[j] ^ i;
      e = rc4(k, e);
    }
    ok = bytesEqual(e, U, 16);
  }
  if (!ok) throw encError("password", "non-empty password required");

  // Resolve the crypt-filter method (V4 /CF+/StmF+/StrF; V<4 is always RC4).
  function methodFor(which) {
    if (V < 4) return "rc4";
    const name = enc[which];
    if (name == null || name === "/Identity") return "identity";
    const cf = enc.CF && typeof enc.CF === "object" ? enc.CF[name.slice(1)] : null;
    const cfm = cf && cf.CFM;
    if (cfm === "/AESV2") return "aes128";
    if (cfm === "/AESV3") return "aes256"; // unusual under V4, handle defensively
    if (cfm === "/Identity") return "identity";
    return "rc4"; // /V2 or unknown ⇒ RC4
  }
  const stmMethod = methodFor("StmF");
  const strMethod = methodFor("StrF");

  function objectKey(objNum, gen, isAes) {
    const ext = new Uint8Array(fileKey.length + 5 + (isAes ? 4 : 0));
    ext.set(fileKey);
    let o = fileKey.length;
    ext[o++] = objNum & 0xff;
    ext[o++] = (objNum >> 8) & 0xff;
    ext[o++] = (objNum >> 16) & 0xff;
    ext[o++] = gen & 0xff;
    ext[o++] = (gen >> 8) & 0xff;
    if (isAes) ext.set([0x73, 0x41, 0x6c, 0x54], o); // "sAlT"
    return md5(ext).subarray(0, Math.min(fileKey.length + 5, 16));
  }
  function decryptWith(method, data, objNum, gen) {
    if (method === "identity") return data;
    if (method === "aes256") return aesDecryptPdfData(fileKey, data);
    const isAes = method === "aes128";
    const key = objectKey(objNum, gen, isAes);
    return isAes ? aesDecryptPdfData(key, data) : rc4(key, data);
  }
  return {
    decryptStream: (b, objNum, gen) => decryptWith(stmMethod, b, objNum, gen),
    decryptString: (b, objNum, gen) => decryptWith(strMethod, b, objNum, gen),
  };
}

// ---------- encoding ----------

function buildWinAnsi() {
  const t = new Array(256).fill(null);
  for (let i = 0x20; i < 0x7f; i++) t[i] = i;
  for (let i = 0xa0; i <= 0xff; i++) t[i] = i; // identical to Latin-1/Unicode
  Object.assign(t, {
    0x80: 0x20ac, 0x82: 0x201a, 0x83: 0x0192, 0x84: 0x201e, 0x85: 0x2026,
    0x86: 0x2020, 0x87: 0x2021, 0x88: 0x02c6, 0x89: 0x2030, 0x8a: 0x0160,
    0x8b: 0x2039, 0x8c: 0x0152, 0x8e: 0x017d, 0x91: 0x2018, 0x92: 0x2019,
    0x93: 0x201c, 0x94: 0x201d, 0x95: 0x2022, 0x96: 0x2013, 0x97: 0x2014,
    0x98: 0x02dc, 0x99: 0x2122, 0x9a: 0x0161, 0x9b: 0x203a, 0x9c: 0x0153,
    0x9e: 0x017e, 0x9f: 0x0178,
  });
  return t;
}

function buildMacRoman() {
  const t = new Array(256).fill(null);
  for (let i = 0x20; i < 0x7f; i++) t[i] = i;
  Object.assign(t, {
    0x80: 0xc4, 0x81: 0xc5, 0x82: 0xc7, 0x83: 0xc9, 0x84: 0xd1, 0x85: 0xd6,
    0x86: 0xdc, 0x87: 0xe1, 0x88: 0xe0, 0x89: 0xe2, 0x8a: 0xe4, 0x8b: 0xe3,
    0x8c: 0xe5, 0x8d: 0xe7, 0x8e: 0xe9, 0x8f: 0xe8, 0x90: 0xea, 0x91: 0xeb,
    0x92: 0xed, 0x93: 0xec, 0x94: 0xee, 0x95: 0xef, 0x96: 0xf1, 0x97: 0xf3,
    0x98: 0xf2, 0x99: 0xf4, 0x9a: 0xf6, 0x9b: 0xf5, 0x9c: 0xfa, 0x9d: 0xf9,
    0x9e: 0xfb, 0x9f: 0xfc, 0xa0: 0x2020, 0xa1: 0xb0, 0xa2: 0xa2, 0xa3: 0xa3,
    0xa4: 0xa7, 0xa5: 0x2022, 0xa6: 0xb6, 0xa7: 0xdf, 0xa8: 0xae, 0xa9: 0xa9,
    0xaa: 0x2122, 0xab: 0xb4, 0xac: 0xa8, 0xad: 0x2260, 0xae: 0xc6, 0xaf: 0xd8,
    0xb0: 0x221e, 0xb1: 0xb1, 0xb2: 0x2264, 0xb3: 0x2265, 0xb4: 0xa5, 0xb5: 0xb5,
    0xb6: 0x2202, 0xb7: 0x2211, 0xb8: 0x220f, 0xb9: 0x3c0, 0xba: 0x222b, 0xbb: 0xaa,
    0xbc: 0xba, 0xbd: 0x3a9, 0xbe: 0xe6, 0xbf: 0xf8, 0xc0: 0xbf, 0xc1: 0xa1,
    0xc2: 0xac, 0xc3: 0x221a, 0xc4: 0x192, 0xc5: 0x2248, 0xc6: 0x2206, 0xc7: 0xab,
    0xc8: 0xbb, 0xc9: 0x2026, 0xca: 0xa0, 0xcb: 0xc0, 0xcc: 0xc3, 0xcd: 0xd5,
    0xce: 0x152, 0xcf: 0x153, 0xd0: 0x2013, 0xd1: 0x2014, 0xd2: 0x201c, 0xd3: 0x201d,
    0xd4: 0x2018, 0xd5: 0x2019, 0xd6: 0xf7, 0xd7: 0x25ca, 0xd8: 0xff, 0xd9: 0x178,
    0xda: 0x2044, 0xdb: 0x20ac, 0xdc: 0x2039, 0xdd: 0x203a, 0xde: 0xfb01, 0xdf: 0xfb02,
    0xe0: 0x2021, 0xe1: 0xb7, 0xe2: 0x201a, 0xe3: 0x201e, 0xe4: 0x2030, 0xe5: 0xc2,
    0xe6: 0xca, 0xe7: 0xc1, 0xe8: 0xcb, 0xe9: 0xc8, 0xea: 0xcd, 0xeb: 0xce,
    0xec: 0xcf, 0xed: 0xcc, 0xee: 0xd3, 0xef: 0xd4, 0xf1: 0xd2, 0xf2: 0xda,
    0xf3: 0xdb, 0xf4: 0xd9, 0xf5: 0x131, 0xf6: 0x2c6, 0xf7: 0x2dc, 0xf8: 0xaf,
    0xf9: 0x2d8, 0xfa: 0x2d9, 0xfb: 0x2da, 0xfc: 0xb8, 0xfd: 0x2dd, 0xfe: 0x2db,
    0xff: 0x2c7,
  });
  return t;
}

function buildStandard() {
  const t = new Array(256).fill(null);
  for (let i = 0x20; i < 0x7f; i++) t[i] = i;
  t[0x27] = 0x2019; // quoteright
  t[0x60] = 0x2018; // quoteleft
  Object.assign(t, {
    0xa1: 0xa1, 0xa2: 0xa2, 0xa3: 0xa3, 0xa4: 0x2044, 0xa5: 0xa5, 0xa6: 0x192,
    0xa7: 0xa7, 0xa8: 0xa4, 0xa9: 0x27, 0xaa: 0x201c, 0xab: 0xab, 0xac: 0x2039,
    0xad: 0x203a, 0xae: 0xfb01, 0xaf: 0xfb02, 0xb1: 0x2013, 0xb2: 0x2020, 0xb3: 0x2021,
    0xb4: 0xb7, 0xb6: 0xb6, 0xb7: 0x2022, 0xb8: 0x201a, 0xb9: 0x201e, 0xba: 0x201d,
    0xbb: 0xbb, 0xbc: 0x2026, 0xbd: 0x2030, 0xbf: 0xbf, 0xc1: 0x60, 0xc2: 0xb4,
    0xc3: 0x2c6, 0xc4: 0x2dc, 0xc5: 0xaf, 0xc6: 0x2d8, 0xc7: 0x2d9, 0xc8: 0xa8,
    0xca: 0x2da, 0xcb: 0xb8, 0xcd: 0x2dd, 0xce: 0x2db, 0xcf: 0x2c7, 0xd0: 0x2014,
    0xe1: 0xc6, 0xe3: 0xaa, 0xe8: 0x141, 0xe9: 0xd8, 0xea: 0x152, 0xeb: 0xba,
    0xf1: 0xe6, 0xf5: 0x131, 0xf8: 0x142, 0xf9: 0xf8, 0xfa: 0x153, 0xfb: 0xdf,
  });
  return t;
}

const WIN_ANSI = buildWinAnsi();
const MAC_ROMAN = buildMacRoman();
const STANDARD = buildStandard();

// Small common-glyph-name -> Unicode table (not a full Adobe Glyph List) for
// /Differences overrides, plus uniXXXX/uXXXX generic fallback.
const GLYPH_NAMES = {
  space: 0x20, exclam: 0x21, quotedbl: 0x22, numbersign: 0x23, dollar: 0x24,
  percent: 0x25, ampersand: 0x26, quotesingle: 0x27, parenleft: 0x28, parenright: 0x29,
  asterisk: 0x2a, plus: 0x2b, comma: 0x2c, hyphen: 0x2d, minus: 0x2d, period: 0x2e,
  slash: 0x2f, zero: 0x30, one: 0x31, two: 0x32, three: 0x33, four: 0x34, five: 0x35,
  six: 0x36, seven: 0x37, eight: 0x38, nine: 0x39, colon: 0x3a, semicolon: 0x3b,
  less: 0x3c, equal: 0x3d, greater: 0x3e, question: 0x3f, at: 0x40,
  bracketleft: 0x5b, backslash: 0x5c, bracketright: 0x5d, asciicircum: 0x5e,
  underscore: 0x5f, grave: 0x60, braceleft: 0x7b, bar: 0x7c, braceright: 0x7d,
  asciitilde: 0x7e, quoteleft: 0x2018, quoteright: 0x2019, quotedblleft: 0x201c,
  quotedblright: 0x201d, quotesinglbase: 0x201a, quotedblbase: 0x201e,
  emdash: 0x2014, endash: 0x2013, bullet: 0x2022, ellipsis: 0x2026, dagger: 0x2020,
  daggerdbl: 0x2021, fi: 0xfb01, fl: 0xfb02, currency: 0xa4, degree: 0xb0,
  cent: 0xa2, sterling: 0xa3, yen: 0xa5, section: 0xa7, paragraph: 0xb6,
  trademark: 0x2122, registered: 0xae, copyright: 0xa9, dieresis: 0xa8, acute: 0xb4,
  cedilla: 0xb8, macron: 0xaf, ring: 0x2da, breve: 0x2d8, caron: 0x2c7,
  ogonek: 0x2db, tilde: 0x2dc, circumflex: 0x2c6, AE: 0xc6, ae: 0xe6,
  OE: 0x152, oe: 0x153, Oslash: 0xd8, oslash: 0xf8, Lslash: 0x141, lslash: 0x142,
  germandbls: 0xdf, dotlessi: 0x131, florin: 0x192, perthousand: 0x2030,
  fraction: 0x2044, guilsinglleft: 0x2039, guilsinglright: 0x203a,
  guillemotleft: 0xab, guillemotright: 0xbb, exclamdown: 0xa1, questiondown: 0xbf,
  plusminus: 0xb1, multiply: 0xd7, divide: 0xf7, logicalnot: 0xac, mu: 0xb5,
  periodcentered: 0xb7,
};

function glyphNameToUnicode(name) {
  if (Object.prototype.hasOwnProperty.call(GLYPH_NAMES, name)) return GLYPH_NAMES[name];
  if (name.length === 1) {
    const c = name.charCodeAt(0);
    if ((c >= 0x41 && c <= 0x5a) || (c >= 0x61 && c <= 0x7a)) return c;
  }
  let m = /^uni([0-9A-Fa-f]{4})$/.exec(name);
  if (m) return parseInt(m[1], 16);
  m = /^u([0-9A-Fa-f]{4,6})$/.exec(name);
  if (m) return parseInt(m[1], 16);
  return null;
}

function parseDifferences(arr) {
  const map = new Map();
  let code = 0;
  for (const item of arr) {
    if (typeof item === "number") code = item;
    else if (typeof item === "string" && item.startsWith("/")) {
      const u = glyphNameToUnicode(item.slice(1));
      if (u != null) map.set(code, String.fromCodePoint(u));
      code++;
    }
  }
  return map;
}

function bytesToInt(u8) {
  let v = 0;
  for (const b of u8) v = v * 256 + b;
  return v;
}
function utf16beToStr(u8) {
  let s = "";
  for (let i = 0; i + 1 < u8.length; i += 2) s += String.fromCharCode((u8[i] << 8) | u8[i + 1]);
  return s;
}
function codepointToStr(cp) {
  return cp > 0xffff ? String.fromCodePoint(cp) : String.fromCharCode(cp);
}

// Scans a /ToUnicode CMap stream for beginbfchar/beginbfrange blocks only —
// not a general PostScript/CMap program interpreter (FR6, §8 of the design).
function parseToUnicodeCMap(bytes) {
  const lex = createLexer(bytes, 0);
  const map = new Map();
  const MAX_RANGE = 65536; // hardening cap against pathological bfrange spans
  while (true) {
    const t = lex.next();
    if (t.type === "eof") break;
    if (t.type === "keyword" && t.value === "beginbfchar") {
      while (true) {
        const a = lex.next();
        if (a.type === "eof" || (a.type === "keyword" && a.value === "endbfchar")) break;
        if (a.type !== "hexstr") continue;
        const b = lex.next();
        if (b.type === "hexstr") map.set(bytesToInt(a.value), utf16beToStr(b.value));
      }
    } else if (t.type === "keyword" && t.value === "beginbfrange") {
      while (true) {
        const a = lex.next();
        if (a.type === "eof" || (a.type === "keyword" && a.value === "endbfrange")) break;
        if (a.type !== "hexstr") continue;
        const loCode = bytesToInt(a.value);
        const b = lex.next();
        if (b.type !== "hexstr") continue;
        const hiCode = bytesToInt(b.value);
        const count = Math.min(hiCode - loCode, MAX_RANGE);
        const c = lex.next();
        if (c.type === "hexstr") {
          const base = bytesToInt(c.value);
          for (let i = 0; i <= count; i++) map.set(loCode + i, codepointToStr(base + i));
        } else if (c.type === "arrStart") {
          const arr = parseArrayBody(lex, 0);
          for (let i = 0; i < arr.length && i <= count; i++) {
            if (arr[i] instanceof Uint8Array) map.set(loCode + i, utf16beToStr(arr[i]));
          }
        }
      }
    }
  }
  return map;
}

// ---------- content stream ----------

// Locates the end of an inline image's binary payload (BI ... ID <data> EI)
// without ever lexing the binary as tokens (FR6b) — trusts a declared /L
// length when plausible, else scans for a whitespace-delimited "EI".
function skipInlineImageData(bytes, dict, dataStart) {
  const L = typeof dict.L === "number" ? dict.L : typeof dict.Length === "number" ? dict.Length : null;
  if (L != null && L >= 0 && dataStart + L <= bytes.length) {
    const q = _scanWhile(bytes, dataStart + L, isWhitespace);
    if (matchKeywordAt(bytes, q, "EI") && (q + 2 >= bytes.length || isWhitespace(bytes[q + 2]) || isDelim(bytes[q + 2]))) {
      return q + 2;
    }
  }
  for (let i = dataStart; i < bytes.length - 1; i++) {
    if (bytes[i] === 0x45 && bytes[i + 1] === 0x49) {
      const beforeOk = i > dataStart && isWhitespace(bytes[i - 1]);
      const afterOk = i + 2 >= bytes.length || isWhitespace(bytes[i + 2]) || isDelim(bytes[i + 2]);
      if (beforeOk && afterOk) return i + 2;
    }
  }
  return bytes.length;
}

function handleInlineImage(bytes, lex) {
  const dict = {};
  while (true) {
    const save = lex.pos;
    const t = lex.next();
    if (t.type === "eof") return bytes.length;
    if (t.type === "keyword" && t.value === "ID") break;
    if (t.type === "name") {
      dict[t.value.slice(1)] = parseValue(lex, 0);
      continue;
    }
    lex.pos = save;
    lex.next(); // skip stray token defensively
  }
  let dataStart = lex.pos;
  if (dataStart < bytes.length && isWhitespace(bytes[dataStart])) dataStart++;
  return skipInlineImageData(bytes, dict, dataStart);
}

// ---------- table reconstruction (REQUIREMENTS-STRUCTURED_FIDELITY) ----------
// The interpreter records each show operation's text-space origin in `frags`.
// Here we group them into visual lines, detect column-aligned regions, and
// render those as an addressed grid (shared with the spreadsheet path). Returns
// null when no table is found so the caller keeps the byte-identical flat-text
// output — conservative reconstruction, no prose regression (FR2/FR3).

const Y_TOL = 3;    // points: fragments within this y-delta share a visual line
const COL_TOL = 18; // points: start-x values within this delta are one column

function groupLines(frags) {
  const sorted = [...frags].sort((a, b) => b.y - a.y || a.x - b.x); // top→down, left→right
  const lines = [];
  for (const f of sorted) {
    const last = lines[lines.length - 1];
    if (last && Math.abs(last.y - f.y) <= Y_TOL) last.frags.push(f);
    else lines.push({ y: f.y, frags: [f] });
  }
  for (const ln of lines) ln.frags.sort((a, b) => a.x - b.x);
  return lines;
}

// Columns supported by ≥2 of the run's lines, so incidental multi-fragment
// prose lines don't read as a table. Returns sorted column-center x values.
function clusterColumns(runLines) {
  const xs = [];
  runLines.forEach((ln, li) => ln.frags.forEach((f) => xs.push({ x: f.x, li })));
  xs.sort((a, b) => a.x - b.x);
  const clusters = [];
  for (const p of xs) {
    const c = clusters[clusters.length - 1];
    if (c && p.x - c.sum / c.n <= COL_TOL) { c.sum += p.x; c.n++; c.lines.add(p.li); }
    else clusters.push({ sum: p.x, n: 1, lines: new Set([p.li]) });
  }
  return clusters.filter((c) => c.lines.size >= 2).map((c) => c.sum / c.n).sort((a, b) => a - b);
}

function nearestCol(cols, x) {
  let best = 0, bestD = Infinity;
  for (let i = 0; i < cols.length; i++) {
    const d = Math.abs(cols[i] - x);
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

function reconstructPage(frags) {
  if (!frags.length) return null;
  const lines = groupLines(frags);
  const out = [];
  let sawTable = false;
  let i = 0;
  while (i < lines.length) {
    // grow a candidate run of consecutive lines that each have ≥2 fragments
    let j = i;
    while (j < lines.length && lines[j].frags.length >= 2) j++;
    if (j - i >= 2) {
      const cols = clusterColumns(lines.slice(i, j));
      if (cols.length >= 2) {
        const rows = lines.slice(i, j).map((ln) => {
          const cells = new Array(cols.length).fill("");
          for (const f of ln.frags) {
            const ci = nearestCol(cols, f.x);
            cells[ci] = cells[ci] ? `${cells[ci]} ${f.str}` : f.str;
          }
          return cells;
        });
        out.push(renderAddressedGrid(rows));
        sawTable = true;
        i = j;
        continue;
      }
    }
    out.push(lines[i].frags.map((f) => f.str).join(" ").replace(/\s+/g, " ").trim());
    i++;
  }
  if (!sawTable) return null;
  return out.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

// ---------- public API ----------

export async function openPdf(data) {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  const { xrefMap, trailer } = await parseXref(bytes);
  if (!trailer || trailer.Root == null) {
    throw new Error("could not locate PDF catalog (/Root) — file structure is not recognizable");
  }

  const objCache = new Map();
  const streamInfo = new Map();
  const decodedStreamCache = new Map();
  const objStmCache = new Map();
  const resolving = new Set();

  // Encryption state. `decryptor` stays null until the /Encrypt dict is parsed
  // and the handler is built (below), so the /Encrypt object itself and the
  // xref-stream parsing that precede setup are never fed through decryption.
  let decryptor = null;
  let encryptObjNum = -1;

  // Recursively decrypts every string (Uint8Array leaf) in a parsed object's
  // value tree in place. Indirect references ({ref,gen}) are left untouched —
  // they resolve to their own objects, decrypted at their own obj#/gen.
  function walkAndDecryptStrings(node, objNum, gen, depth = 0) {
    if (depth > MAX_NESTING || !node || typeof node !== "object") return;
    if (node instanceof Uint8Array || typeof node.ref === "number") return;
    const keys = Array.isArray(node) ? node.keys() : Object.keys(node);
    for (const k of keys) {
      const v = node[k];
      if (v instanceof Uint8Array) node[k] = decryptor.decryptString(v, objNum, gen);
      else if (v && typeof v === "object") walkAndDecryptStrings(v, objNum, gen, depth + 1);
    }
  }

  async function resolveObject(objNum) {
    if (objCache.has(objNum)) return objCache.get(objNum);
    if (resolving.has(objNum)) return null; // reference-cycle guard (FR10b)
    const entry = xrefMap.get(objNum);
    if (!entry) return null;
    resolving.add(objNum);
    try {
      let result = null;
      if (entry.type === 1) {
        const parsed = await parseIndirectObjectAt(bytes, entry.offset, async (ref) => {
          const v = await resolveObject(ref);
          return typeof v === "number" ? v : null;
        });
        let streamRaw = parsed.streamRaw;
        // Decrypt only when a handler exists; never the /Encrypt object itself
        // and never an xref stream (both are always stored unencrypted).
        const isXRefStream = parsed.value && parsed.value.Type === "/XRef";
        if (decryptor && objNum !== encryptObjNum && !isXRefStream) {
          const gen = entry.gen || 0;
          if (parsed.value && typeof parsed.value === "object") {
            walkAndDecryptStrings(parsed.value, objNum, gen);
          }
          if (streamRaw) streamRaw = decryptor.decryptStream(streamRaw, objNum, gen);
        }
        if (streamRaw) streamInfo.set(objNum, { dict: parsed.value, rawBytes: streamRaw });
        result = parsed.value;
      } else if (entry.type === 2) {
        const stm = await getObjStm(entry.streamObjNum);
        const pair = stm.pairs[entry.indexInStream];
        if (pair) result = parseValue(createLexer(stm.bytes, stm.first + pair.offset), 0);
      }
      objCache.set(objNum, result);
      return result;
    } finally {
      resolving.delete(objNum);
    }
  }

  async function resolve(v) {
    if (v && typeof v === "object" && typeof v.ref === "number" && !Array.isArray(v)) {
      return resolveObject(v.ref);
    }
    return v;
  }

  async function get(dict, key) {
    if (!dict || typeof dict !== "object") return undefined;
    return resolve(dict[key]);
  }

  // Encryption setup (FR7/FR8). Resolve the /Encrypt dict (which may be an
  // indirect ref) and build the handler BEFORE any content object is read, so
  // decryption is active for the page tree. Runs while `decryptor` is still
  // null, so the /Encrypt dict's own key strings (/O,/U) are never decrypted.
  if (trailer.Encrypt != null) {
    if (trailer.Encrypt && typeof trailer.Encrypt.ref === "number") {
      encryptObjNum = trailer.Encrypt.ref;
    }
    const encDict = await resolve(trailer.Encrypt);
    const idArr = Array.isArray(trailer.ID) ? trailer.ID : null;
    const idBytes = idArr && idArr[0] instanceof Uint8Array ? idArr[0] : new Uint8Array(0);
    decryptor = await buildSecurityHandler(encDict, idBytes); // throws {code:"encrypted"|"password"}
  }

  async function decodeStreamByRef(objNum) {
    if (decodedStreamCache.has(objNum)) return decodedStreamCache.get(objNum);
    await resolveObject(objNum); // ensures streamInfo is populated if it's a stream
    const info = streamInfo.get(objNum);
    if (!info) return new Uint8Array(0);
    const out = await applyFilters(info.dict, info.rawBytes);
    decodedStreamCache.set(objNum, out);
    return out;
  }

  async function getObjStm(streamObjNum) {
    if (objStmCache.has(streamObjNum)) return objStmCache.get(streamObjNum);
    const dict = await resolveObject(streamObjNum);
    const raw = await decodeStreamByRef(streamObjNum);
    const n = dict?.N || 0;
    const first = dict?.First || 0;
    const lex = createLexer(raw, 0);
    const pairs = [];
    for (let i = 0; i < n; i++) {
      const a = lex.next();
      const b = lex.next();
      if (a.type !== "num" || b.type !== "num") break;
      pairs.push({ num: a.value, offset: b.value });
    }
    const result = { pairs, first, bytes: raw };
    objStmCache.set(streamObjNum, result);
    return result;
  }

  // ---------- page tree ----------

  const MAX_TREE_DEPTH = 64;
  const pageList = [];

  async function walkPageTree(ref, inheritedResources, ancestors, depth) {
    if (depth > MAX_TREE_DEPTH) return;
    if (!ref || typeof ref.ref !== "number") return;
    if (ancestors.has(ref.ref)) return;
    const node = await resolveObject(ref.ref);
    if (!node) return;
    const resources = node.Resources != null ? await get(node, "Resources") : inheritedResources;
    if (Array.isArray(node.Kids)) {
      const nextAncestors = new Set(ancestors);
      nextAncestors.add(ref.ref);
      for (const kidRef of node.Kids) {
        await walkPageTree(kidRef, resources, nextAncestors, depth + 1);
      }
    } else {
      pageList.push({ pageRef: ref, resources });
    }
  }

  const catalog = await resolve(trailer.Root);
  if (catalog && catalog.Pages) {
    await walkPageTree(catalog.Pages, null, new Set(), 0);
  }

  // ---------- font decoding ----------

  const fontDecoderCache = new WeakMap();

  function defaultDecode(strBytes) {
    let out = "";
    for (const b of strBytes) {
      const u = WIN_ANSI[b];
      if (u != null) out += String.fromCodePoint(u);
    }
    return out;
  }

  async function buildFontDecoder(fontDict) {
    let toUnicodeMap = null;
    const toUnicodeRef = fontDict.ToUnicode;
    if (toUnicodeRef && typeof toUnicodeRef.ref === "number") {
      try {
        const cmapBytes = await decodeStreamByRef(toUnicodeRef.ref);
        toUnicodeMap = parseToUnicodeCMap(cmapBytes);
      } catch {
        toUnicodeMap = null;
      }
    }
    const isType0 = fontDict.Subtype === "/Type0";
    let baseTable = null;
    let diffs = null;
    if (!isType0) {
      let encName = null;
      const enc = fontDict.Encoding;
      if (typeof enc === "string") encName = enc;
      else if (enc && typeof enc === "object") {
        if (typeof enc.BaseEncoding === "string") encName = enc.BaseEncoding;
        if (Array.isArray(enc.Differences)) diffs = parseDifferences(enc.Differences);
      }
      baseTable = encName === "/MacRomanEncoding" ? MAC_ROMAN : encName === "/StandardEncoding" ? STANDARD : WIN_ANSI;
    }
    const codeWidth = isType0 ? 2 : 1;
    return function decode(strBytes) {
      let out = "";
      for (let i = 0; i + codeWidth <= strBytes.length; i += codeWidth) {
        let code = 0;
        for (let k = 0; k < codeWidth; k++) code = (code << 8) | strBytes[i + k];
        if (toUnicodeMap && toUnicodeMap.has(code)) {
          out += toUnicodeMap.get(code);
          continue;
        }
        if (isType0) continue; // no ToUnicode on a CID font: accepted gap, contributes nothing
        if (diffs && diffs.has(code)) {
          out += diffs.get(code);
          continue;
        }
        const u = baseTable[code];
        if (u != null) out += String.fromCodePoint(u);
      }
      return out;
    };
  }

  // ---------- content-stream text extraction ----------

  async function interpretContentStream(bytes2, resources) {
    const lex = createLexer(bytes2, 0);
    const fontDict = resources ? await get(resources, "Font") : null;
    let currentDecoder = defaultDecode;
    let currentFontSize = 12;
    let stack = [];
    const out = [];
    // Parallel positional capture: the flat `out` path stays untouched for
    // prose; `frags` records each show op's text-space origin (curX,curY) so a
    // table region can be reconstructed afterward (REQUIREMENTS-STRUCTURED_FIDELITY).
    let curX = 0, curY = 0, leading = 0;
    const frags = [];
    function recordFrag(str) {
      if (str) frags.push({ x: curX, y: curY, str });
    }

    function pushSpace() {
      if (out.length && out[out.length - 1] !== " " && out[out.length - 1] !== "\n") out.push(" ");
    }
    function showText(v) {
      if (v instanceof Uint8Array) {
        const s = currentDecoder(v);
        out.push(s);
        recordFrag(s);
      }
    }

    while (true) {
      const tok = lex.next();
      if (tok.type === "eof") break;
      if (tok.type === "num") {
        stack.push(tok.value);
      } else if (tok.type === "name" || tok.type === "str" || tok.type === "hexstr") {
        stack.push(tok.value);
      } else if (tok.type === "arrStart") {
        stack.push(parseArrayBody(lex, 0));
      } else if (tok.type === "dictStart") {
        stack.push(parseDictBody(lex, 0));
      } else if (tok.type === "keyword") {
        switch (tok.value) {
          case "BT":
            curX = 0; curY = 0;
            stack = [];
            break;
          case "ET":
            stack = [];
            break;
          case "Tf": {
            const size = stack[stack.length - 1];
            const name = stack[stack.length - 2];
            if (typeof size === "number") currentFontSize = size;
            if (typeof name === "string" && name.startsWith("/") && fontDict) {
              const fontRef = fontDict[name.slice(1)];
              if (fontRef != null) {
                const fontObj = await resolve(fontRef);
                if (fontObj) {
                  if (fontDecoderCache.has(fontObj)) currentDecoder = fontDecoderCache.get(fontObj);
                  else {
                    currentDecoder = await buildFontDecoder(fontObj);
                    fontDecoderCache.set(fontObj, currentDecoder);
                  }
                }
              }
            }
            stack = [];
            break;
          }
          case "Td": {
            const tx = stack[stack.length - 2];
            const ty = stack[stack.length - 1];
            if (typeof tx === "number") curX += tx;
            if (typeof ty === "number") curY += ty;
            if (typeof ty === "number" && Math.abs(ty) > currentFontSize * 0.2) out.push("\n");
            else pushSpace();
            stack = [];
            break;
          }
          case "TD": {
            const tx = stack[stack.length - 2];
            const ty = stack[stack.length - 1];
            if (typeof ty === "number") leading = -ty;
            if (typeof tx === "number") curX += tx;
            if (typeof ty === "number") curY += ty;
            out.push("\n");
            stack = [];
            break;
          }
          case "T*":
            curY -= leading;
            out.push("\n");
            stack = [];
            break;
          case "TL": {
            const v = stack[stack.length - 1];
            if (typeof v === "number") leading = v;
            stack = [];
            break;
          }
          case "Tm": {
            // a b c d e f — e,f are the absolute translation (text origin).
            const e = stack[stack.length - 2];
            const f = stack[stack.length - 1];
            if (typeof e === "number") curX = e;
            if (typeof f === "number") curY = f;
            stack = [];
            break;
          }
          case "Tj":
            showText(stack[stack.length - 1]);
            stack = [];
            break;
          case "'":
            curY -= leading;
            out.push("\n");
            showText(stack[stack.length - 1]);
            stack = [];
            break;
          case '"':
            curY -= leading;
            out.push("\n");
            showText(stack[stack.length - 1]);
            stack = [];
            break;
          case "TJ": {
            const arr = stack[stack.length - 1];
            if (Array.isArray(arr)) {
              let joined = "";
              for (const item of arr) {
                if (item instanceof Uint8Array) {
                  const s = currentDecoder(item);
                  out.push(s);
                  joined += s;
                } else if (typeof item === "number" && item <= -120) {
                  out.push(" ");
                  joined += " ";
                }
              }
              recordFrag(joined);
            }
            stack = [];
            break;
          }
          case "BI":
            lex.pos = handleInlineImage(bytes2, lex);
            stack = [];
            break;
          default:
            stack = [];
        }
      }
    }
    // Reconstruct column-aligned regions as grids; fall back to the untouched
    // flat text when no table is detected (no prose regression).
    const rebuilt = reconstructPage(frags);
    return rebuilt != null ? rebuilt : out.join("");
  }

  async function extractPageText(pageDict, resources) {
    const contentsRef = pageDict.Contents;
    if (contentsRef == null) return "";
    const contents = Array.isArray(contentsRef) ? contentsRef : [contentsRef];
    const parts = [];
    for (const cRef of contents) {
      if (!cRef || typeof cRef.ref !== "number") continue;
      try {
        parts.push(await decodeStreamByRef(cRef.ref));
      } catch {
        // an unreadable content stream (corrupt, encrypted, unsupported
        // filter) contributes no text rather than failing the whole page
      }
    }
    if (parts.length === 0) return "";
    const total = parts.reduce((a, b) => a + b.length + 1, 0);
    const merged = new Uint8Array(total);
    let o = 0;
    for (const p of parts) {
      merged.set(p, o);
      o += p.length;
      merged[o] = 0x20;
      o++;
    }
    try {
      return await interpretContentStream(merged, resources);
    } catch {
      return "";
    }
  }

  return {
    numPages: pageList.length,
    async getPageText(pageNo) {
      if (pageNo < 1 || pageNo > pageList.length) return "";
      const { pageRef, resources } = pageList[pageNo - 1];
      const pageDict = await resolveObject(pageRef.ref);
      if (!pageDict) return "";
      return extractPageText(pageDict, resources);
    },
  };
}
