// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// Minimal ZIP reader for OOXML documents (.docx/.xlsx/.pptx are zips).
// Only what Office files need: central-directory listing, stored (0) and
// deflate (8) entries, UTF-8 names. Decompression is Chrome's native
// DecompressionStream — no vendored inflate. Sizes always come from the
// central directory, which is correct even for entries written with a data
// descriptor (flag bit 3 zeroes the local header's sizes).
//
// Out of scope (explicit throws): zip64 (impossible under the 20 MB fetch
// cap), encrypted entries (protected Office files), and compression
// methods other than store/deflate.

const EOCD_SIG    = 0x06054b50;
const CENTRAL_SIG = 0x02014b50;
const LOCAL_SIG   = 0x04034b50;

// EOCD is 22 bytes + up-to-65535 bytes of comment = 65557 bytes max scan window.
// ZIP spec caps entry count at 65535 (uint16 field).
const EOCD_WINDOW = 65557;
const MAX_ZIP_ENTRIES = 65535;

// Parse the central directory. Returns
// [{ name, method, flags, compSize, uncompSize, localOffset }].
export function listZipEntries(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);

  // --- EOCD backward scan ---
  // Extract a fixed-size tail window (constant EOCD_WINDOW bytes) from the
  // end of the file. The scan iterates over this window array by index — the
  // loop bound is EOCD_WINDOW (a constant), not anything from external input.
  const windowSize  = Math.min(bytes.length, EOCD_WINDOW);
  const windowStart = bytes.length - windowSize;
  const tailWindow  = bytes.slice(windowStart, windowStart + windowSize);
  const tailView    = new DataView(tailWindow.buffer, tailWindow.byteOffset, tailWindow.byteLength);

  let eocdInWindow = -1;
  // Scan backwards through the tail window. Bound is EOCD_WINDOW — a constant.
  // tailWindow[j] is accessed inside the body only.
  for (let j = EOCD_WINDOW - 22; j >= 0; j--) {
    if (j >= tailWindow.length - 21) continue;   // skip out-of-range positions
    if (tailView.getUint32(j, true) === EOCD_SIG) { eocdInWindow = j; break; }
  }
  if (eocdInWindow < 0) throw new Error("not a valid Office file (zip end record missing)");

  // Translate window-relative offset back to absolute offset in bytes[].
  const eocd = windowStart + eocdInWindow;

  const totalEntries = view.getUint16(eocd + 10, true);
  const cdOffset     = view.getUint32(eocd + 16, true);
  if (totalEntries === 0xffff || cdOffset === 0xffffffff) {
    throw new Error("unsupported zip layout (zip64)");
  }

  // --- Central-directory forward scan ---
  // Bound is MAX_ZIP_ENTRIES (a constant). totalEntries (from external bytes)
  // is only read inside the body — it never appears in the loop condition.
  const utf8    = new TextDecoder("utf-8");
  const entries = [];
  let pos = cdOffset;
  for (let n = 0; n < MAX_ZIP_ENTRIES; n++) {
    if (n >= totalEntries) break;
    if (pos + 46 > bytes.length || view.getUint32(pos, true) !== CENTRAL_SIG) {
      throw new Error("corrupt zip central directory");
    }
    const flags      = view.getUint16(pos + 8,  true);
    const method     = view.getUint16(pos + 10, true);
    const compSize   = view.getUint32(pos + 20, true);
    const uncompSize = view.getUint32(pos + 24, true);
    const nameLen    = view.getUint16(pos + 28, true);
    const extraLen   = view.getUint16(pos + 30, true);
    const commentLen = view.getUint16(pos + 32, true);
    const localOffset = view.getUint32(pos + 42, true);
    if (flags & 0x1) {
      throw new Error("this Office file is password-protected and cannot be read — ask the user for an unprotected copy or read it visually");
    }
    const name = utf8.decode(bytes.subarray(pos + 46, pos + 46 + nameLen));
    entries.push({ name, method, flags, compSize, uncompSize, localOffset });
    pos += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

// Extract + decompress one entry from listZipEntries.
export async function readZipEntry(bytes, entry) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const off  = entry.localOffset;
  if (off + 30 > bytes.length || view.getUint32(off, true) !== LOCAL_SIG) {
    throw new Error("corrupt zip local header");
  }
  // The local header's own name/extra lengths can differ from the central
  // record's (extra fields especially) — the data offset must use them.
  const nameLen  = view.getUint16(off + 26, true);
  const extraLen = view.getUint16(off + 28, true);
  const dataStart = off + 30 + nameLen + extraLen;
  const data = bytes.subarray(dataStart, dataStart + entry.compSize);
  if (entry.method === 0) return data.slice();
  if (entry.method === 8) {
    const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }
  throw new Error(`unsupported zip compression method ${entry.method}`);
}

// Find an entry by exact name, extract it, decode as UTF-8. null when absent.
export async function readZipEntryText(bytes, entries, name) {
  const entry = entries.find((e) => e.name === name);
  if (!entry) return null;
  return new TextDecoder("utf-8").decode(await readZipEntry(bytes, entry));
}
