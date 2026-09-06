// SPDX-License-Identifier: MIT
/**
 * zipReader — Lightweight pure-JS ZIP extractor using browser-native APIs.
 *
 * Reads ZIP archives from an ArrayBuffer without any npm dependencies.
 * Uses DataView for binary parsing and DecompressionStream('deflate-raw')
 * for inflating compressed entries.
 *
 * Supports:
 *   - Stored entries (compression method 0)
 *   - Deflate entries (compression method 8)
 *   - Central directory fallback for data-descriptor entries
 *   - UTF-8 filenames
 *
 * Exports:
 *   zipExtract(arrayBuffer, targetFilename)  → Uint8Array | null
 *   zipListEntries(arrayBuffer)              → string[]
 */

const LOCAL_FILE_HEADER_SIG  = 0x04034b50; // PK\x03\x04
const CENTRAL_DIR_SIG        = 0x02014b50; // PK\x01\x02
const END_OF_CENTRAL_DIR_SIG = 0x06054b50; // PK\x05\x06

const decoder = new TextDecoder("utf-8");

// ── Central Directory parser ────────────────────────────────────────────────
// Used as a fallback when local file headers have zero-length compressed size
// (data descriptor entries, bit 3 of general purpose flag).

function parseCentralDirectory(buf) {
  const view = new DataView(buf);
  const len  = buf.byteLength;

  // Locate End of Central Directory record — scan backwards from EOF.
  // EOCD is at least 22 bytes; comment field can extend it up to 65557 bytes.
  let eocdOffset = -1;
  const scanStart = Math.max(0, len - 65557);
  for (let i = len - 22; i >= scanStart; i--) {
    if (view.getUint32(i, true) === END_OF_CENTRAL_DIR_SIG) {
      eocdOffset = i;
      break;
    }
  }
  if (eocdOffset < 0) return null;

  const cdOffset = view.getUint32(eocdOffset + 16, true); // offset of start of central directory
  const cdSize   = view.getUint32(eocdOffset + 12, true); // size of central directory

  // Walk central directory entries
  const entries = [];
  let pos = cdOffset;
  const cdEnd = cdOffset + cdSize;

  while (pos < cdEnd && pos + 46 <= len) {
    if (view.getUint32(pos, true) !== CENTRAL_DIR_SIG) break;

    const method       = view.getUint16(pos + 10, true);
    const compSize     = view.getUint32(pos + 20, true);
    const uncompSize   = view.getUint32(pos + 24, true);
    const nameLen      = view.getUint16(pos + 28, true);
    const extraLen     = view.getUint16(pos + 30, true);
    const commentLen   = view.getUint16(pos + 32, true);
    const localOffset  = view.getUint32(pos + 42, true);

    const nameBytes = new Uint8Array(buf, pos + 46, nameLen);
    const filename  = decoder.decode(nameBytes);

    entries.push({ filename, method, compSize, uncompSize, localOffset });

    pos += 46 + nameLen + extraLen + commentLen;
  }

  return entries;
}

// ── Inflate a deflate-raw compressed buffer ─────────────────────────────────

async function inflateRaw(compressedBytes) {
  const ds     = new DecompressionStream("deflate-raw");
  const writer = ds.writable.getWriter();
  const reader = ds.readable.getReader();

  // Write compressed data and close
  writer.write(compressedBytes).catch(() => {});
  writer.close().catch(() => {});

  // Collect decompressed chunks — cap at 512 MB to prevent unbounded growth
  // from untrusted compressed input (Checkmarx: Unchecked Input For Loop Condition)
  const MAX_DECOMP = 512 * 1024 * 1024;
  const chunks = [];
  let totalLen = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    totalLen += value.byteLength;
    if (totalLen > MAX_DECOMP) throw new Error("Decompressed content exceeds 512 MB limit");
  }

  // Merge into a single Uint8Array
  const result = new Uint8Array(totalLen);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(new Uint8Array(chunk.buffer || chunk), offset);
    offset += chunk.byteLength;
  }
  return result;
}

// ── Read a single entry from its local file header position ─────────────────

async function readLocalEntry(buf, localOffset, cdEntry) {
  const view    = new DataView(buf);
  const len     = buf.byteLength;

  if (localOffset + 30 > len) return null;
  if (view.getUint32(localOffset, true) !== LOCAL_FILE_HEADER_SIG) return null;

  const method   = view.getUint16(localOffset + 8, true);
  const nameLen  = view.getUint16(localOffset + 26, true);
  const extraLen = view.getUint16(localOffset + 28, true);

  // Prefer central directory sizes (reliable even with data descriptors)
  let compSize = cdEntry ? cdEntry.compSize : view.getUint32(localOffset + 18, true);

  const dataStart = localOffset + 30 + nameLen + extraLen;

  if (method === 0) {
    // Stored — no compression
    const size = cdEntry ? cdEntry.uncompSize : compSize;
    if (dataStart + size > len) return null;
    return new Uint8Array(buf, dataStart, size);
  }

  if (method === 8) {
    // Deflate
    if (compSize === 0 && cdEntry) compSize = cdEntry.compSize;
    if (compSize === 0 || dataStart + compSize > len) return null;
    const compressed = new Uint8Array(buf, dataStart, compSize);
    return inflateRaw(compressed);
  }

  // Unsupported compression method
  return null;
}

// ── Parse local file headers sequentially ───────────────────────────────────

function* iterateLocalHeaders(buf) {
  const view = new DataView(buf);
  const len  = buf.byteLength;
  let pos = 0;

  while (pos + 30 <= len) {
    if (view.getUint32(pos, true) !== LOCAL_FILE_HEADER_SIG) break;

    const gpFlag   = view.getUint16(pos + 6, true);
    const method   = view.getUint16(pos + 8, true);
    const compSize = view.getUint32(pos + 18, true);
    const nameLen  = view.getUint16(pos + 26, true);
    const extraLen = view.getUint16(pos + 28, true);

    const nameBytes = new Uint8Array(buf, pos + 30, nameLen);
    const filename  = decoder.decode(nameBytes);
    const hasDD     = (gpFlag & 0x08) !== 0;

    const dataStart = pos + 30 + nameLen + extraLen;

    yield {
      offset: pos,
      filename,
      method,
      compSize,
      dataStart,
      hasDataDescriptor: hasDD,
    };

    // Advance past this entry
    const entryDataSize = compSize > 0 ? compSize : 0;
    let nextPos = dataStart + entryDataSize;

    // If data descriptor is present, skip it (12 or 16 bytes)
    if (hasDD && compSize > 0 && nextPos + 4 <= len) {
      // Data descriptor may or may not have the signature 0x08074b50
      if (view.getUint32(nextPos, true) === 0x08074b50) {
        nextPos += 16; // signature(4) + crc(4) + compSize(4) + uncompSize(4)
      } else {
        nextPos += 12; // crc(4) + compSize(4) + uncompSize(4)
      }
    }

    pos = nextPos;
  }
}

// ── Public API ──────────────────────────────────────────────────────────────

/**
 * Extract a single file from a ZIP archive.
 *
 * @param {ArrayBuffer} arrayBuffer — the ZIP file bytes
 * @param {string}      targetFilename — path inside the ZIP (e.g. "word/document.xml")
 * @returns {Promise<Uint8Array|null>} — decompressed file content, or null if not found
 */
export async function zipExtract(arrayBuffer, targetFilename) {
  const buf = arrayBuffer;
  const target = targetFilename.replace(/^\//, ""); // normalize leading slash

  // First attempt: scan local file headers for a direct match with valid sizes
  for (const entry of iterateLocalHeaders(buf)) {
    if (entry.filename !== target) continue;

    if (entry.compSize > 0 || entry.method === 0) {
      return readLocalEntry(buf, entry.offset, null);
    }

    // compSize is 0 and method is deflate — need central directory
    break;
  }

  // Fallback: use central directory for reliable sizes
  const cdEntries = parseCentralDirectory(buf);
  if (!cdEntries) return null;

  const cdEntry = cdEntries.find(e => e.filename === target);
  if (!cdEntry) return null;

  return readLocalEntry(buf, cdEntry.localOffset, cdEntry);
}

/**
 * List all file entries in a ZIP archive.
 *
 * @param {ArrayBuffer} arrayBuffer — the ZIP file bytes
 * @returns {string[]} — array of filenames/paths inside the ZIP
 */
export function zipListEntries(arrayBuffer) {
  // Prefer central directory (authoritative, handles all edge cases)
  const cdEntries = parseCentralDirectory(arrayBuffer);
  if (cdEntries) return cdEntries.map(e => e.filename);

  // Fallback to local headers
  const names = [];
  for (const entry of iterateLocalHeaders(arrayBuffer)) {
    names.push(entry.filename);
  }
  return names;
}
