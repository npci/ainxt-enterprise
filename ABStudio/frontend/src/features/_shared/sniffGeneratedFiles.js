// SPDX-License-Identifier: Apache-2.0
// Detect file artifacts mentioned in plain-text agent replies.
//
// The Workflow / Agent factory chat endpoints don't return a structured
// `generated_files` payload (unlike the agent runner), so when an LLM
// emits something like "The file is saved at /tmp/Report.docx" the
// frontend has nothing to anchor a download chip to. This helper sniffs
// the message text for paths that end in a known artifact extension and
// returns canonical { filename, download_url } records suitable for the
// shared FileDownloadCard / chip strip.
//
// Backend mapping: the agent sandbox CWD is pinned to GENERATED_FILES_DIR
// (= ABStudio/tmp). So any basename the agent writes there is reachable
// at `/generated-files/<basename>`. We rely on the backend to reject
// names that don't exist (404/410) — we never invent paths.
//
// Returns: Array<{ filename: string, download_url: string }>

// Extensions we offer a download chip for. Keep this in sync with the
// media-type map in backend/app/main.py::download_generated_file.
const DOWNLOADABLE_EXTS = [
    'pdf', 'docx', 'doc', 'pptx', 'ppt', 'xlsx', 'xls',
    'csv', 'txt', 'md', 'json', 'zip', 'html', 'png', 'jpg', 'jpeg',
];

// Subset of DOWNLOADABLE_EXTS that are user-facing deliverables.
// Intermediate tool outputs (json, txt helpers, logs) are excluded so the
// download strip shows only the final document the user actually wants.
export const PRIMARY_DOWNLOAD_EXTS = new Set([
    'pdf', 'docx', 'doc', 'pptx', 'ppt',
    'xlsx', 'xls', 'csv', 'zip', 'html',
    'png', 'jpg', 'jpeg', 'mp4', 'mp3',
]);

// Filename character class: letters, digits, dot, underscore, dash, space.
// We DON'T let `/` slip in so we always match the leaf name only. The
// optional leading path part is matched separately and discarded.
const _extAlt = DOWNLOADABLE_EXTS.join('|');
const FILE_PATH_RE = new RegExp(
    // Optional path prefix (anything non-whitespace ending with `/` or `\`),
    // then a filename, then a known extension. Word-boundary on the extension
    // keeps "report.docx." or "report.docx," from being eaten with the
    // trailing punctuation.
    `(?:[^\\s'"\`()<>]*[/\\\\])?([\\w.\\-]+\\.(?:${_extAlt}))\\b`,
    'gi',
);

/**
 * Sniff agent message text for downloadable file artifacts.
 *
 * @param {string} text  message content as emitted by the LLM
 * @param {Iterable<string>} [excludeNames]  filenames that must NOT be treated
 *        as generated artifacts — typically the user's uploaded attachments for
 *        the current thread. When the assistant echoes an uploaded filename in
 *        its prose (e.g. "Summary of Report.xlsx"), the sniffer would otherwise
 *        fabricate a /generated-files/<name> download card that points at a file
 *        that was never generated (dead / "expired" link). Matching is
 *        case-insensitive. Optional — omitting it preserves the old behaviour.
 * @returns {Array<{filename: string, download_url: string}>}
 */
export function sniffGeneratedFiles(text, excludeNames) {
    if (!text || typeof text !== 'string') return [];
    // Normalise the exclude list into a lowercased Set for O(1) lookups. Accepts
    // an array, a Set, or undefined.
    const excluded = new Set();
    if (excludeNames) {
        for (const name of excludeNames) {
            if (typeof name === 'string' && name) excluded.add(name.toLowerCase());
        }
    }
    const seen = new Set();
    const out  = [];
    let m;
    // RegExp.exec with /g keeps state on the RegExp object; reset just in
    // case this module was hot-reloaded mid-iteration.
    FILE_PATH_RE.lastIndex = 0;
    while ((m = FILE_PATH_RE.exec(text)) !== null) {
        const filename = m[1];
        if (!filename) continue;
        // Dedupe — the same artifact often appears twice (once in a
        // sentence, once in a fenced code block).
        const key = filename.toLowerCase();
        if (seen.has(key)) continue;
        // Skip user-uploaded input files — they are not generated artifacts and
        // have no /generated-files/ URL, so a download card for them 404/410s.
        if (excluded.has(key)) continue;
        seen.add(key);
        out.push({
            filename,
            download_url: `/generated-files/${encodeURIComponent(filename)}`,
        });
    }
    return out;
}

// Remove a bare `/generated-files/<name>` path (and any leading
// "Download[ it here]:" label) from assistant prose. Callers use this when a
// download card is already rendered for the artifact, so the raw path doesn't
// linger as unclickable text. The `(?<![\]\(])` lookbehind leaves markdown
// links (`](/generated-files/...)`) intact — those become real anchors.
const _BARE_GEN_PATH_RE = /[^\S\n]*(?:download(?:\s+it\s+here)?\s*:?[^\S\n]*)?(?<![\]\(])\/generated-files\/\S+/gi;

export function stripBareGeneratedPaths(text) {
    if (!text || typeof text !== 'string') return text;
    return text
        .replace(_BARE_GEN_PATH_RE, '')
        .replace(/[^\S\n]+\n/g, '\n')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
}

// Remove a MARKDOWN-LINK form generated-file reference, e.g.
// `[Download Report.docx](/generated-files/Report.docx)` (with or without an
// API-base prefix). Callers use this so that when a styled FileDownloadCard
// button is rendered for the artifact, the assistant's inline markdown link
// isn't ALSO shown as a plain blue text link — which looked inconsistent next
// to the button card (bare-named files got a button, markdown-linked ones got a
// plain link). We strip the whole `[label](url)` token plus any leading
// "Download" label word so only the button card remains.
const _GEN_MD_LINK_RE = /[^\S\n]*(?:download(?:\s+it\s+here)?\s*:?[^\S\n]*)?\[[^\]]*\]\((?:https?:\/\/[^)]*)?\/generated-files\/[^)]*\)/gi;

export function stripGeneratedMarkdownLinks(text) {
    if (!text || typeof text !== 'string') return text;
    return text
        .replace(_GEN_MD_LINK_RE, '')
        .replace(/[^\S\n]+\n/g, '\n')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
}
