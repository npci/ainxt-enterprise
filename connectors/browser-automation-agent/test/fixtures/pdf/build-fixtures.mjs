// Dev-only fixture builder (not shipped) — hand-constructs minimal PDF byte
// structures so lib/pdf.js can be tested against exact, known structure
// (classic xref, xref streams, object streams, FlateDecode+predictor,
// ToUnicode, inline images, image-only pages, LZWDecode, and empty-password
// encryption in RC4 / AES-128 / AES-256) without depending on any external
// PDF-authoring tool. The encryption side reuses lib/pdf-crypto.js so the
// fixtures and the reader share one crypto implementation (the crypto itself
// is independently vector-tested in test/crypto-kat.mjs).
import { writeFileSync } from "node:fs";
import { deflateSync } from "node:zlib";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { pdfLegacyDigest128, rc4, aesCbcEncryptNoPad, sha } from "../../../lib/pdf-crypto.js";

const OUT = dirname(fileURLToPath(import.meta.url));

function enc(s) {
  return Buffer.from(s, "latin1");
}

// Concatenate indirect objects (each already-formatted "N 0 obj ... endobj"
// buffer, keyed by object number) into a full classic-xref PDF.
function assembleClassic(objects, rootNum, extraTrailer = "") {
  const header = enc("%PDF-1.4\n%\xE2\xE3\xCF\xD3\n");
  const maxNum = Math.max(...objects.keys());
  const chunks = [header];
  const offsets = new Array(maxNum + 1).fill(null);
  let pos = header.length;
  for (let n = 1; n <= maxNum; n++) {
    const buf = objects.get(n);
    if (!buf) continue;
    offsets[n] = pos;
    chunks.push(buf);
    pos += buf.length;
  }
  const xrefOffset = pos;
  let xref = `xref\n0 ${maxNum + 1}\n`;
  xref += `0000000000 65535 f \n`;
  for (let n = 1; n <= maxNum; n++) {
    const off = offsets[n];
    if (off == null) {
      xref += `0000000000 00000 f \n`;
    } else {
      xref += `${String(off).padStart(10, "0")} 00000 n \n`;
    }
  }
  chunks.push(enc(xref));
  const trailer = `trailer\n<< /Size ${maxNum + 1} /Root ${rootNum} 0 R${extraTrailer} >>\nstartxref\n${xrefOffset}\n%%EOF\n`;
  chunks.push(enc(trailer));
  return Buffer.concat(chunks);
}

function obj(num, dictStr) {
  return Buffer.concat([enc(`${num} 0 obj\n${dictStr}\nendobj\n`)]);
}

function streamObj(num, dictStr, dataBuf) {
  return Buffer.concat([
    enc(`${num} 0 obj\n${dictStr}\nstream\n`),
    dataBuf,
    enc(`\nendstream\nendobj\n`),
  ]);
}

// ---------- LZW encoder (inverse of lib/pdf.js lzwDecode) ----------
// Variable-width 9–12 bit, MSB-first, EarlyChange=1, clear=256 / EOD=257.
// Verified to round-trip against the reader's decoder and to reproduce the
// ISO 32000-1 §7.4.4.2 worked example.
function lzwEncode(data, earlyChange = 1) {
  const out = [];
  let bitBuffer = 0, bitCount = 0;
  function write(code, width) {
    bitBuffer = (bitBuffer << width) | code;
    bitCount += width;
    while (bitCount >= 8) { bitCount -= 8; out.push((bitBuffer >> bitCount) & 0xff); }
  }
  let dict, dictSize, width;
  function reset() {
    dict = new Map();
    for (let i = 0; i < 256; i++) dict.set(String.fromCharCode(i), i);
    dictSize = 258; width = 9;
  }
  reset();
  write(256, width);
  let w = "";
  for (const b of data) {
    const c = String.fromCharCode(b);
    const wc = w + c;
    if (dict.has(wc)) { w = wc; continue; }
    write(dict.get(w), width);
    dict.set(wc, dictSize++);
    if (dictSize + earlyChange >= (1 << width) && width < 12) width++;
    w = c;
  }
  if (w !== "") write(dict.get(w), width);
  write(257, width);
  if (bitCount > 0) out.push((bitBuffer << (8 - bitCount)) & 0xff);
  return Buffer.from(out);
}

// ---------- standard security handler encryption (empty passwords) ----------

const PAD = new Uint8Array([
  0x28, 0xbf, 0x4e, 0x5e, 0x4e, 0x75, 0x8a, 0x41, 0x64, 0x00, 0x4e, 0x56, 0xff, 0xfa, 0x01, 0x08,
  0x2e, 0x2e, 0x00, 0xb6, 0xd0, 0x68, 0x3e, 0x80, 0x2f, 0x0c, 0xa9, 0xfe, 0x64, 0x53, 0x69, 0x7a,
]);
const u8 = (arr) => new Uint8Array(arr);
const ZERO16 = new Uint8Array(16);
const FIXED_IV = u8([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]); // deterministic build

function cat(parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const o = new Uint8Array(n);
  let k = 0;
  for (const p of parts) { o.set(p, k); k += p.length; }
  return o;
}
function hexStr(arr) {
  return "<" + [...arr].map((b) => b.toString(16).padStart(2, "0")).join("") + ">";
}
function p32le(n) {
  const v = n | 0;
  return u8([v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff]);
}
function xorKey(key, i) {
  const k = new Uint8Array(key.length);
  for (let j = 0; j < k.length; j++) k[j] = key[j] ^ i;
  return k;
}
function padPwd(pw) {
  const o = new Uint8Array(32);
  const n = Math.min(pw.length, 32);
  o.set(pw.subarray(0, n));
  o.set(PAD.subarray(0, 32 - n), n);
  return o;
}
function ownerKey(ownerPwd, keyLen, R) {
  let h = pdfLegacyDigest128(padPwd(ownerPwd));
  if (R >= 3) for (let i = 0; i < 50; i++) h = pdfLegacyDigest128(h.subarray(0, keyLen));
  return h.subarray(0, keyLen);
}
function computeO(userPwd, ownerPwd, keyLen, R) {
  const ok = ownerKey(ownerPwd, keyLen, R);
  let o = rc4(ok, padPwd(userPwd));
  if (R >= 3) for (let i = 1; i <= 19; i++) o = rc4(xorKey(ok, i), o);
  return o;
}
function fileKeyR234(userPwd, O, P, id, keyLen, R) {
  let h = pdfLegacyDigest128(cat([padPwd(userPwd), O, p32le(P), id]));
  if (R >= 3) for (let i = 0; i < 50; i++) h = pdfLegacyDigest128(h.subarray(0, keyLen));
  return h.subarray(0, keyLen);
}
function computeU(fileKey, id, R) {
  if (R === 2) return rc4(fileKey, PAD);
  let u = rc4(fileKey, pdfLegacyDigest128(cat([PAD, id])));
  for (let i = 1; i <= 19; i++) u = rc4(xorKey(fileKey, i), u);
  const out = new Uint8Array(32);
  out.set(u.subarray(0, 16));
  out.set(PAD.subarray(0, 16), 16); // arbitrary 16-byte tail (spec: any padding)
  return out;
}
function objKey(fileKey, num, gen, isAes) {
  const ext = new Uint8Array(fileKey.length + 5 + (isAes ? 4 : 0));
  ext.set(fileKey);
  let o = fileKey.length;
  ext[o++] = num & 0xff; ext[o++] = (num >> 8) & 0xff; ext[o++] = (num >> 16) & 0xff;
  ext[o++] = gen & 0xff; ext[o++] = (gen >> 8) & 0xff;
  if (isAes) ext.set([0x73, 0x41, 0x6c, 0x54], o);
  return pdfLegacyDigest128(ext).subarray(0, Math.min(fileKey.length + 5, 16));
}
function pkcs7(data) {
  const pad = 16 - (data.length % 16);
  const o = new Uint8Array(data.length + pad);
  o.set(data);
  o.fill(pad, data.length);
  return o;
}
function aesEncPdf(key, data) {
  return cat([FIXED_IV, aesCbcEncryptNoPad(key, FIXED_IV, pkcs7(data))]);
}
function encryptStream(cipher, fileKey, num, gen, data) {
  if (cipher === "rc4") return rc4(objKey(fileKey, num, gen, false), data);
  if (cipher === "aes128") return aesEncPdf(objKey(fileKey, num, gen, true), data);
  if (cipher === "aes256") return aesEncPdf(fileKey, data); // V5: file key used directly
  throw new Error("bad cipher");
}
// R6 hardened hash (ISO 32000-2 Algorithm 2.B) — mirror of lib/pdf.js hash2B.
async function hash2B(password, salt, udata) {
  let K = await sha(256, cat([password, salt, udata]));
  for (let round = 1; ; round++) {
    const block = cat([password, K, udata]);
    const K1 = new Uint8Array(block.length * 64);
    for (let i = 0; i < 64; i++) K1.set(block, i * block.length);
    const E = aesCbcEncryptNoPad(K.subarray(0, 16), K.subarray(16, 32), K1);
    let mod = 0;
    for (let i = 0; i < 16; i++) mod += E[i];
    mod %= 3;
    K = await sha(mod === 0 ? 256 : mod === 1 ? 384 : 512, E);
    if (round >= 64 && E[E.length - 1] <= round - 32) break;
  }
  return K.subarray(0, 32);
}

const EMPTY = new Uint8Array(0);

// Standard 4-object single-page skeleton (catalog, pages, page, Helvetica
// font) whose /Contents is object 5; the caller supplies object 5 (the
// possibly-encrypted content stream) and the /Encrypt object + trailer extra.
function pageSkeleton() {
  const objects = new Map();
  objects.set(1, obj(1, `<< /Type /Catalog /Pages 2 0 R >>`));
  objects.set(2, obj(2, `<< /Type /Pages /Kids [3 0 R] /Count 1 >>`));
  objects.set(3, obj(3, `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>`));
  objects.set(4, obj(4, `<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>`));
  return objects;
}

// ---------- fixture 1: simple.pdf ----------
// Classic xref, 2 pages, uncompressed content streams. Page 1: plain WinAnsi
// text with Tj/TJ (word-spacing via large negative TJ adjustment) and a
// Td/T* line break. Page 2: a font using /Differences to remap one code.
{
  const objects = new Map();
  objects.set(1, obj(1, `<< /Type /Catalog /Pages 2 0 R >>`));
  objects.set(
    2,
    obj(2, `<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>`)
  );
  objects.set(
    3,
    obj(
      3,
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>`
    )
  );
  objects.set(
    4,
    obj(4, `<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>`)
  );
  const page1Content = enc(
    `BT\n/F1 24 Tf\n72 700 Td\n(Hello,) Tj\n[( ) -400 (World!)] TJ\n0 -20 TD\n(Second line.) Tj\nET`
  );
  objects.set(5, streamObj(5, `<< /Length ${page1Content.length} >>`, page1Content));
  objects.set(
    6,
    obj(
      6,
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F2 7 0 R >> >> /Contents 8 0 R >>`
    )
  );
  // /Differences remaps code 65 ('A') to glyph "currency" (WinAnsi 0xA4,
  // U+00A4) so the golden text must show the currency sign, not "A".
  objects.set(
    7,
    obj(
      7,
      `<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding << /BaseEncoding /WinAnsiEncoding /Differences [65 /currency] >> >>`
    )
  );
  const page2Content = enc(`BT\n/F2 18 Tf\n72 700 Td\n(A) Tj\nET`);
  objects.set(8, streamObj(8, `<< /Length ${page2Content.length} >>`, page2Content));
  writeFileSync(join(OUT, "simple.pdf"), assembleClassic(objects, 1));
}

// ---------- fixture 2: flate-predictor.pdf ----------
// Classic xref; content stream is FlateDecode-compressed AND PNG-predictor
// (Up, type 2) encoded to exercise both filter unwrapping and predictor math.
{
  const raw = enc(`BT\n/F1 24 Tf\n72 700 Td\n(Predicted and deflated text.) Tj\nET`);
  // PNG "Up" filter (type 2) per row; treat the whole stream as one row of
  // width = raw.length, 1 "color" byte-sample (arbitrary but must match what
  // the decoder assumes for Colors=1/BitsPerComponent=8/Columns=raw.length).
  const columns = raw.length;
  const filtered = Buffer.concat([Buffer.from([2]), raw]); // row tag 2 = Up, prev row = all zero -> Up filter is a no-op vs. zero row
  const compressed = deflateSync(filtered);
  const objects = new Map();
  objects.set(1, obj(1, `<< /Type /Catalog /Pages 2 0 R >>`));
  objects.set(2, obj(2, `<< /Type /Pages /Kids [3 0 R] /Count 1 >>`));
  objects.set(
    3,
    obj(
      3,
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>`
    )
  );
  objects.set(4, obj(4, `<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>`));
  objects.set(
    5,
    streamObj(
      5,
      `<< /Length ${compressed.length} /Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns ${columns} /Colors 1 /BitsPerComponent 8 >> >>`,
      compressed
    )
  );
  writeFileSync(join(OUT, "flate-predictor.pdf"), assembleClassic(objects, 1));
}

// ---------- fixture 3: xrefstream-objstm.pdf ----------
// Modern-style file: dict objects 1-4 live inside an ObjStm (object 6),
// content stream (object 5) is a regular FlateDecode stream, and the file's
// own cross-reference table is an xref stream (object 7, uncompressed for
// simplicity) rather than a classic "xref" table.
{
  const catalog = `<< /Type /Catalog /Pages 2 0 R >>`;
  const pages = `<< /Type /Pages /Kids [3 0 R] /Count 1 >>`;
  const page = `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>`;
  const font = `<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>`;

  // Build the ObjStm body: header "objnum offset objnum offset ..." then
  // concatenated object values (bare, no "N 0 obj"/"endobj" wrapper).
  const bodies = [catalog, pages, page, font];
  const nums = [1, 2, 3, 4];
  let headerParts = [];
  let offsetAcc = 0;
  const bodyBufs = bodies.map((b) => enc(b + "\n"));
  for (let i = 0; i < nums.length; i++) {
    headerParts.push(`${nums[i]} ${offsetAcc}`);
    offsetAcc += bodyBufs[i].length;
  }
  const objStmHeader = enc(headerParts.join(" ") + " ");
  const objStmBody = Buffer.concat([objStmHeader, ...bodyBufs]);
  const objStmCompressed = deflateSync(objStmBody);

  const content = enc(`BT\n/F1 20 Tf\n72 700 Td\n(Xref stream and object stream text.) Tj\nET`);
  const contentCompressed = deflateSync(content);

  // Layout: header, obj5 (content stream), obj6 (ObjStm), obj7 (xref stream).
  const header = enc("%PDF-1.5\n%\xE2\xE3\xCF\xD3\n");
  const obj5 = streamObj(5, `<< /Length ${contentCompressed.length} /Filter /FlateDecode >>`, contentCompressed);
  const obj5Offset = header.length;
  const obj6Offset = obj5Offset + obj5.length;
  const obj6 = streamObj(
    6,
    `<< /Type /ObjStm /N ${nums.length} /First ${objStmHeader.length} /Length ${objStmCompressed.length} /Filter /FlateDecode >>`,
    objStmCompressed
  );
  const obj7Offset = obj6Offset + obj6.length;

  // xref stream entries for objects 0..7 (8 entries), W = [1,4,2].
  const W = [1, 4, 2];
  function xrefEntry(type, f2, f3) {
    const b = Buffer.alloc(W[0] + W[1] + W[2]);
    let o = 0;
    b.writeUInt8(type, o);
    o += W[0];
    b.writeUIntBE(f2, o, W[1]);
    o += W[1];
    b.writeUIntBE(f3, o, W[2]);
    return b;
  }
  const entries = [
    xrefEntry(0, 0, 65535), // obj 0: free
    xrefEntry(2, 6, 0), // obj 1 -> ObjStm 6, index 0
    xrefEntry(2, 6, 1), // obj 2 -> ObjStm 6, index 1
    xrefEntry(2, 6, 2), // obj 3 -> ObjStm 6, index 2
    xrefEntry(2, 6, 3), // obj 4 -> ObjStm 6, index 3
    xrefEntry(1, obj5Offset, 0), // obj 5
    xrefEntry(1, obj6Offset, 0), // obj 6
    xrefEntry(1, obj7Offset, 0), // obj 7 (self, the xref stream)
  ];
  const xrefData = Buffer.concat(entries);
  const obj7 = streamObj(
    7,
    `<< /Type /XRef /Size 8 /W [${W.join(" ")}] /Root 1 0 R /Length ${xrefData.length} >>`,
    xrefData
  );

  const trailerTail = enc(`startxref\n${obj7Offset}\n%%EOF\n`);
  const full = Buffer.concat([header, obj5, obj6, obj7, trailerTail]);
  writeFileSync(join(OUT, "xrefstream-objstm.pdf"), full);
}

// ---------- fixture 4: tounicode-cjk.pdf ----------
// A Type0/Identity-H composite font with an embedded /ToUnicode CMap mapping
// two 2-byte codes to CJK codepoints (no embedded font program needed since
// only text extraction, not rendering, is in scope).
{
  const toUnicode = enc(
    [
      "/CIDInit /ProcSet findresource begin",
      "12 dict begin",
      "begincmap",
      "1 begincodespacerange",
      "<0000> <FFFF>",
      "endcodespacerange",
      "2 beginbfchar",
      "<0001> <4F60>",
      "<0002> <597D>",
      "endbfchar",
      "endcmap",
      "end",
      "end",
    ].join("\n")
  );
  const objects = new Map();
  objects.set(1, obj(1, `<< /Type /Catalog /Pages 2 0 R >>`));
  objects.set(2, obj(2, `<< /Type /Pages /Kids [3 0 R] /Count 1 >>`));
  objects.set(
    3,
    obj(
      3,
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 6 0 R >>`
    )
  );
  objects.set(
    4,
    obj(
      4,
      `<< /Type /Font /Subtype /Type0 /BaseFont /Identity /Encoding /Identity-H /DescendantFonts [5 0 R] /ToUnicode 7 0 R >>`
    )
  );
  objects.set(
    5,
    obj(
      5,
      `<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Identity /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 1000 >>`
    )
  );
  // Two 2-byte codes 0x0001 0x0002 shown via hex string.
  const content = enc(`BT\n/F1 20 Tf\n72 700 Td\n<00010002> Tj\nET`);
  objects.set(6, streamObj(6, `<< /Length ${content.length} >>`, content));
  objects.set(7, streamObj(7, `<< /Length ${toUnicode.length} >>`, toUnicode));
  writeFileSync(join(OUT, "tounicode-cjk.pdf"), assembleClassic(objects, 1));
}

// ---------- fixture 5: inline-image.pdf ----------
// Text, then an inline image with a declared /L length whose binary payload
// deliberately contains the byte sequence "EI" surrounded by whitespace (a
// false-positive trap for scanners that ignore /L), then more text.
{
  const imgBytes = Buffer.from([0x00, 0x45, 0x49, 0x20, 0xff, 0x10, 0x20, 0x45, 0x49, 0x00]); // contains " EI " mid-stream
  const before = enc(`BT\n/F1 18 Tf\n72 700 Td\n(Before image.) Tj\nET\n`);
  const bi = Buffer.concat([
    enc(`BI\n/W 2\n/H 5\n/BPC 8\n/CS /G\n/L ${imgBytes.length}\n/F /AHx\nID `),
    imgBytes,
    enc(`\nEI\n`),
  ]);
  const after = enc(`BT\n/F1 18 Tf\n72 650 Td\n(After image.) Tj\nET`);
  const content = Buffer.concat([before, bi, after]);
  const objects = new Map();
  objects.set(1, obj(1, `<< /Type /Catalog /Pages 2 0 R >>`));
  objects.set(2, obj(2, `<< /Type /Pages /Kids [3 0 R] /Count 1 >>`));
  objects.set(
    3,
    obj(
      3,
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>`
    )
  );
  objects.set(4, obj(4, `<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>`));
  objects.set(5, streamObj(5, `<< /Length ${content.length} >>`, content));
  writeFileSync(join(OUT, "inline-image.pdf"), assembleClassic(objects, 1));
}

// ---------- fixture 6: scanned.pdf ----------
// A page whose content stream only paints an image XObject — no text-show
// operators at all — must trigger the "likely scanned images" fallback.
{
  const content = enc(`q\n200 0 0 200 100 500 cm\n/Im1 Do\nQ`);
  const objects = new Map();
  objects.set(1, obj(1, `<< /Type /Catalog /Pages 2 0 R >>`));
  objects.set(2, obj(2, `<< /Type /Pages /Kids [3 0 R] /Count 1 >>`));
  objects.set(
    3,
    obj(
      3,
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >>`
    )
  );
  const imgData = Buffer.from([0, 0, 0, 255, 255, 255, 0, 0, 0, 255, 255, 255]);
  objects.set(
    4,
    streamObj(
      4,
      `<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Length ${imgData.length} >>`,
      imgData
    )
  );
  objects.set(5, streamObj(5, `<< /Length ${content.length} >>`, content));
  writeFileSync(join(OUT, "scanned.pdf"), assembleClassic(objects, 1));
}

function writeGolden(name, numPages, text) {
  writeFileSync(join(OUT, "golden", `${name}.txt`), `numPages=${numPages}\n--- page 1 ---\n${text}\n`);
}

// ---------- fixture 7: lzw.pdf ----------
// Content stream compressed with LZWDecode (variable-width, EarlyChange=1).
{
  const content = enc(`BT\n/F1 20 Tf\n72 700 Td\n(LZW compressed content stream.) Tj\nET`);
  const compressed = lzwEncode(content);
  const objects = pageSkeleton();
  objects.set(5, streamObj(5, `<< /Length ${compressed.length} /Filter /LZWDecode >>`, compressed));
  writeFileSync(join(OUT, "lzw.pdf"), assembleClassic(objects, 1));
  writeGolden("lzw", 1, "LZW compressed content stream.");
}

// ---------- fixture 8: encrypt-rc4.pdf (empty password, V2/R3, RC4) ----------
{
  const id = u8([0x9a, 0x2b, 0x14, 0x77, 0xc3, 0x50, 0xd1, 0x0e, 0x66, 0xf2, 0x81, 0x39, 0xab, 0x5c, 0x7d, 0x04]);
  const P = -3904, R = 3, V = 2, keyLen = 16;
  const O = computeO(EMPTY, EMPTY, keyLen, R);
  const fileKey = fileKeyR234(EMPTY, O, P, id, keyLen, R);
  const U = computeU(fileKey, id, R);
  const content = enc(`BT\n/F1 24 Tf\n72 700 Td\n(Secret text via RC4.) Tj\nET`);
  const encContent = Buffer.from(encryptStream("rc4", fileKey, 5, 0, content));
  const objects = pageSkeleton();
  objects.set(5, streamObj(5, `<< /Length ${encContent.length} >>`, encContent));
  objects.set(6, obj(6, `<< /Filter /Standard /V ${V} /R ${R} /Length 128 /P ${P} /O ${hexStr(O)} /U ${hexStr(U)} >>`));
  const extra = ` /Encrypt 6 0 R /ID [${hexStr(id)} ${hexStr(id)}]`;
  writeFileSync(join(OUT, "encrypt-rc4.pdf"), assembleClassic(objects, 1, extra));
  writeGolden("encrypt-rc4", 1, "Secret text via RC4.");
}

// ---------- fixture 9: encrypt-aes128.pdf (empty password, V4/R4, AESV2) ----------
{
  const id = u8([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x00]);
  const P = -3904, R = 4, V = 4, keyLen = 16;
  const O = computeO(EMPTY, EMPTY, keyLen, R);
  const fileKey = fileKeyR234(EMPTY, O, P, id, keyLen, R);
  const U = computeU(fileKey, id, R);
  const content = enc(`BT\n/F1 24 Tf\n72 700 Td\n(Secret text via AES-128.) Tj\nET`);
  const encContent = Buffer.from(encryptStream("aes128", fileKey, 5, 0, content));
  const objects = pageSkeleton();
  objects.set(5, streamObj(5, `<< /Length ${encContent.length} >>`, encContent));
  const cf = `/CF << /StdCF << /CFM /AESV2 /Length 16 >> >> /StmF /StdCF /StrF /StdCF`;
  objects.set(6, obj(6, `<< /Filter /Standard /V ${V} /R ${R} /Length 128 /P ${P} /O ${hexStr(O)} /U ${hexStr(U)} ${cf} >>`));
  const extra = ` /Encrypt 6 0 R /ID [${hexStr(id)} ${hexStr(id)}]`;
  writeFileSync(join(OUT, "encrypt-aes128.pdf"), assembleClassic(objects, 1, extra));
  writeGolden("encrypt-aes128", 1, "Secret text via AES-128.");
}

// ---------- fixture 10: encrypt-aes256.pdf (empty password, V5/R6, AESV3) ----------
{
  const id = u8([0xde, 0xad, 0xbe, 0xef, 0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef, 0xfe, 0xdc, 0xba, 0x98]);
  const P = -3904;
  const fileKey = u8(Array.from({ length: 32 }, (_, i) => (i * 7 + 3) & 0xff));
  const vSaltU = u8([1, 2, 3, 4, 5, 6, 7, 8]);
  const kSaltU = u8([9, 10, 11, 12, 13, 14, 15, 16]);
  const hashU = await hash2B(EMPTY, vSaltU, EMPTY);
  const U = cat([hashU, vSaltU, kSaltU]);
  const ikeyU = await hash2B(EMPTY, kSaltU, EMPTY);
  const UE = aesCbcEncryptNoPad(ikeyU, ZERO16, fileKey);
  const vSaltO = u8([17, 18, 19, 20, 21, 22, 23, 24]);
  const kSaltO = u8([25, 26, 27, 28, 29, 30, 31, 32]);
  const hashO = await hash2B(EMPTY, vSaltO, U);
  const O = cat([hashO, vSaltO, kSaltO]);
  const ikeyO = await hash2B(EMPTY, kSaltO, U);
  const OE = aesCbcEncryptNoPad(ikeyO, ZERO16, fileKey);
  const perms = new Uint8Array(16);
  perms.set(p32le(P));
  perms.set([0xff, 0xff, 0xff, 0xff], 4);
  perms[8] = 0x54; // 'T' — EncryptMetadata true
  perms[9] = 0x61; perms[10] = 0x64; perms[11] = 0x62; // 'a','d','b'
  const Perms = aesCbcEncryptNoPad(fileKey, ZERO16, perms);
  const content = enc(`BT\n/F1 24 Tf\n72 700 Td\n(Confidential AES-256 text.) Tj\nET`);
  const encContent = Buffer.from(encryptStream("aes256", fileKey, 5, 0, content));
  const objects = pageSkeleton();
  objects.set(5, streamObj(5, `<< /Length ${encContent.length} >>`, encContent));
  const cf = `/CF << /StdCF << /CFM /AESV3 /Length 32 >> >> /StmF /StdCF /StrF /StdCF`;
  objects.set(
    6,
    obj(6, `<< /Filter /Standard /V 5 /R 6 /Length 256 /P ${P} /O ${hexStr(O)} /U ${hexStr(U)} /OE ${hexStr(OE)} /UE ${hexStr(UE)} /Perms ${hexStr(Perms)} ${cf} >>`)
  );
  const extra = ` /Encrypt 6 0 R /ID [${hexStr(id)} ${hexStr(id)}]`;
  writeFileSync(join(OUT, "encrypt-aes256.pdf"), assembleClassic(objects, 1, extra));
  writeGolden("encrypt-aes256", 1, "Confidential AES-256 text.");
}

// ---------- fixture 11: encrypt-password.pdf (NON-empty user password) ----------
// A real user password ("secret") means the empty-password key fails the /U
// check, so openPdf must throw {code:"password"} rather than emit garbage.
{
  const id = u8([0x0f, 0x1e, 0x2d, 0x3c, 0x4b, 0x5a, 0x69, 0x78, 0x87, 0x96, 0xa5, 0xb4, 0xc3, 0xd2, 0xe1, 0xf0]);
  const P = -3904, R = 3, V = 2, keyLen = 16;
  const userPwd = enc("secret"), ownerPwd = enc("owner");
  const O = computeO(userPwd, ownerPwd, keyLen, R);
  const fileKey = fileKeyR234(userPwd, O, P, id, keyLen, R);
  const U = computeU(fileKey, id, R);
  const content = enc(`BT\n/F1 24 Tf\n72 700 Td\n(You should never see this.) Tj\nET`);
  const encContent = Buffer.from(encryptStream("rc4", fileKey, 5, 0, content));
  const objects = pageSkeleton();
  objects.set(5, streamObj(5, `<< /Length ${encContent.length} >>`, encContent));
  objects.set(6, obj(6, `<< /Filter /Standard /V ${V} /R ${R} /Length 128 /P ${P} /O ${hexStr(O)} /U ${hexStr(U)} >>`));
  const extra = ` /Encrypt 6 0 R /ID [${hexStr(id)} ${hexStr(id)}]`;
  writeFileSync(join(OUT, "encrypt-password.pdf"), assembleClassic(objects, 1, extra));
  // No text golden — the harness asserts the expected throw code instead.
  writeFileSync(join(OUT, "golden", "encrypt-password.throws"), "password\n");
}

console.log("Fixtures written to", OUT);
