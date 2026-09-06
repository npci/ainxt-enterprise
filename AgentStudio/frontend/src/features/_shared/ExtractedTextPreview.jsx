// SPDX-License-Identifier: MIT
import { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';

/**
 * Read-only modal that shows the OCR pipeline's extracted text, the
 * engine label, and any warnings. Used by attachment chips so the user
 * can verify what the model will actually see BEFORE sending.
 *
 * Why this is important for OCR: chip-only UX hid the difference between
 * a successful extraction and one where the parser returned a sentinel
 * or OCR mangled the text. A preview surfaces both cases.
 *
 * Why a portal: Build Studio's split-pane layout uses
 * ``position: relative; overflow: hidden`` on the preview panel
 * (AgentEditor.jsx:1535) and on the workflow chat-panel. Rendering the
 * modal inline inside that subtree clips it to the panel and creates a
 * new stacking context — the overlay only dimmed part of the screen and
 * the modal looked broken (see screenshot from 2026-06-30). Portaling
 * to ``document.body`` escapes both issues so the modal covers the
 * whole viewport regardless of where the chip lives.
 *
 * Props
 *  - open:           bool — controls visibility
 *  - onClose:        () => void
 *  - filename:       str
 *  - text:           extracted markdown
 *  - engine:         "text-layer" | "rapidocr" | "vision" | "mixed" | etc.
 *  - warnings:       string[]  — pipeline warnings (camelot missing, etc.)
 *  - imagesCount:    number    — embedded images OCR'd
 *  - tablesCount:    number    — tables extracted
 *  - cacheHit:       bool
 */
export default function ExtractedTextPreview(props) {
    const {
        open, onClose, filename, text, engine,
        warnings = [], imagesCount = 0, tablesCount = 0, cacheHit = false,
    } = props;
    const [copied, setCopied] = useState(false);
    // Two view modes: "rendered" parses the markdown into headings + code
    // blocks (nicer for documents with `## Page 1` etc.); "raw" shows the
    // exact text that will be sent to the model (useful for debugging
    // OCR fidelity).  Default to rendered because most users just want
    // a readable preview.
    const [viewMode, setViewMode] = useState('rendered');

    // Esc closes the modal — standard a11y affordance and mirrors the rest
    // of Build Studio's modals (RunSettingsStrip, GenerateInstructionsModal).
    useEffect(() => {
        if (!open) return undefined;
        function onKey(e) {
            if (e.key === 'Escape') onClose();
        }
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [open, onClose]);

    // Lock body scroll while the modal is open so the workflow canvas /
    // chat-pane don't scroll behind the overlay.
    useEffect(() => {
        if (!open) return undefined;
        const prev = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        return () => { document.body.style.overflow = prev; };
    }, [open]);

    if (!open) return null;
    // SSR / test environments without document.body short-circuit to null.
    if (typeof document === 'undefined' || !document.body) return null;

    async function copy() {
        try {
            await navigator.clipboard.writeText(text || '');
            setCopied(true);
            setTimeout(() => setCopied(false), 1600);
        } catch {
            // Clipboard API can fail in sandboxed iframes — fall back to
            // a select-all hint via the textarea below.
        }
    }

    const modalNode = (
        <div
            role="dialog"
            aria-modal="true"
            aria-label={`Extracted text for ${filename}`}
            style={overlayStyle}
            onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
        >
            <div style={modalStyle} onClick={(e) => e.stopPropagation()}>
                <header style={headerStyle}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                        <strong style={{
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                        }}>{filename}</strong>
                        {/* engine label intentionally hidden from end users —
                            the chip/preview surface should not reveal which
                            extraction path (text-layer / OCR / vision) the
                            backend picked; users only care that the file was
                            read correctly. EngineBadge is still exported for
                            debug/internal screens that may opt back in. */}
                    </div>
                    <button type="button" onClick={onClose} style={closeBtnStyle} aria-label="Close">
                        ×
                    </button>
                </header>
                <div style={metaStyle}>
                    <Pill label={`${(text || '').length.toLocaleString()} chars`} />
                    {imagesCount > 0 && <Pill label={`${imagesCount} image${imagesCount > 1 ? 's' : ''} OCR'd`} />}
                    {tablesCount > 0 && <Pill label={`${tablesCount} table${tablesCount > 1 ? 's' : ''}`} />}
                    {cacheHit && <Pill label="cache hit" tone="muted" />}
                </div>
                {warnings.length > 0 && (
                    <details style={warningsStyle}>
                        <summary style={{ cursor: 'pointer', color: '#b45309' }}>
                            ⚠ {warnings.length} warning{warnings.length > 1 ? 's' : ''}
                        </summary>
                        <ul style={{ margin: '6px 0 0 18px', padding: 0 }}>
                            {warnings.map((w, i) => (
                                <li key={i} style={{ fontSize: 12, color: '#92400e' }}>{w}</li>
                            ))}
                        </ul>
                    </details>
                )}
                <div style={tabStripStyle}>
                    <button
                        type="button"
                        onClick={() => setViewMode('rendered')}
                        style={viewMode === 'rendered' ? tabActiveStyle : tabStyle}
                    >Document</button>
                    <button
                        type="button"
                        onClick={() => setViewMode('raw')}
                        style={viewMode === 'raw' ? tabActiveStyle : tabStyle}
                    >Raw text</button>
                </div>
                {viewMode === 'rendered' ? (
                    <div style={renderedStyle}>
                        {(text || '').trim()
                            ? renderMarkdown(text)
                            : <p style={{ color: '#9ca3af', fontStyle: 'italic' }}>(no text extracted)</p>}
                    </div>
                ) : (
                    <textarea
                        readOnly
                        value={text || '(no text extracted)'}
                        style={textAreaStyle}
                        onFocus={(e) => e.target.select()}
                    />
                )}
                <footer style={footerStyle}>
                    <button type="button" onClick={copy} style={primaryBtnStyle}>
                        {copied ? '✓ Copied' : 'Copy to clipboard'}
                    </button>
                    <button type="button" onClick={onClose} style={secondaryBtnStyle}>
                        Close
                    </button>
                </footer>
            </div>
        </div>
    );

    return createPortal(modalNode, document.body);
}


export function EngineBadge({ engine, cacheHit }) {
    if (!engine) return null;
    const tone = ENGINE_TONES[engine] || ENGINE_TONES.default;
    return (
        <span style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 3,
            padding: '1px 6px',
            borderRadius: 9999,
            background: tone.bg,
            color: tone.fg,
            fontSize: 10,
            fontWeight: 600,
            border: `1px solid ${tone.border}`,
            whiteSpace: 'nowrap',
        }} title={`Extraction engine: ${engine}${cacheHit ? ' (cache hit)' : ''}`}>
            {engine}
            {cacheHit ? '·cache' : ''}
        </span>
    );
}


function Pill({ label, tone = 'default' }) {
    const colors = tone === 'muted'
        ? { bg: '#f3f4f6', fg: '#6b7280' }
        : { bg: '#e0e7ff', fg: '#3730a3' };
    return (
        <span style={{
            display: 'inline-block',
            padding: '2px 8px',
            borderRadius: 9999,
            background: colors.bg,
            color: colors.fg,
            fontSize: 11,
        }}>{label}</span>
    );
}


const ENGINE_TONES = {
    'text-layer': { bg: '#ecfdf5', fg: '#047857', border: '#a7f3d0' },
    'structured': { bg: '#ecfdf5', fg: '#047857', border: '#a7f3d0' },
    'rapidocr':   { bg: '#eff6ff', fg: '#1d4ed8', border: '#bfdbfe' },
    'salvage':    { bg: '#fff7ed', fg: '#9a3412', border: '#fed7aa' },
    'mixed':      { bg: '#f5f3ff', fg: '#6d28d9', border: '#ddd6fe' },
    'vision':     { bg: '#fdf2f8', fg: '#9d174d', border: '#fbcfe8' },
    'image-empty':{ bg: '#fef2f2', fg: '#b91c1c', border: '#fecaca' },
    'cached':     { bg: '#f3f4f6', fg: '#374151', border: '#d1d5db' },
    'default':    { bg: '#f3f4f6', fg: '#374151', border: '#d1d5db' },
};


const overlayStyle = {
    // 9999 > any Build Studio panel z-index. The modal is portalled to
    // document.body so it always covers the whole viewport, including
    // the workflow canvas on the left of the split-pane layout.
    position: 'fixed', inset: 0, zIndex: 9999,
    background: 'rgba(15, 23, 42, 0.55)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    padding: 20,
};
const modalStyle = {
    // Wider + taller (square-ish, ~1:1 aspect) so longer documents
    // (multi-page scans, contracts) are readable without horizontal
    // truncation. Falls back to 95vw on small screens.
    width: 'min(960px, 95vw)',
    height: 'min(820px, 92vh)',
    maxHeight: '92vh',
    display: 'flex', flexDirection: 'column',
    background: '#fff', borderRadius: 12,
    border: '1px solid #e5e7eb',
    boxShadow: '0 20px 50px rgba(0,0,0,0.25)',
    overflow: 'hidden',
};
const headerStyle = {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '10px 14px', borderBottom: '1px solid #e5e7eb',
    gap: 10,
};
const closeBtnStyle = {
    border: 'none', background: 'transparent',
    fontSize: 22, cursor: 'pointer', color: '#6b7280',
    width: 28, height: 28, borderRadius: 6,
};
const metaStyle = {
    display: 'flex', gap: 6, padding: '8px 14px 0', flexWrap: 'wrap',
};
const warningsStyle = {
    margin: '8px 14px 0', padding: '6px 10px',
    background: '#fffbeb', border: '1px solid #fde68a',
    borderRadius: 6, fontSize: 12,
};
const textAreaStyle = {
    flex: 1, margin: '0 14px 10px',
    minHeight: 280, padding: 12,
    border: '1px solid #e5e7eb', borderRadius: 8,
    background: '#fafafa', color: '#111827',
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: 11, lineHeight: 1.55, resize: 'none',
    whiteSpace: 'pre-wrap',
};
const tabStripStyle = {
    display: 'flex', gap: 4,
    padding: '6px 14px 0',
    borderBottom: '1px solid #e5e7eb',
};
const tabStyle = {
    border: 'none', background: 'transparent',
    padding: '6px 12px', fontSize: 12, color: '#6b7280',
    cursor: 'pointer', borderRadius: '6px 6px 0 0',
    borderBottom: '2px solid transparent',
};
const tabActiveStyle = {
    ...tabStyle,
    color: '#4f46e5', fontWeight: 600,
    borderBottom: '2px solid #4f46e5',
};
const renderedStyle = {
    flex: 1, margin: '10px 14px',
    padding: '14px 18px',
    border: '1px solid #e5e7eb', borderRadius: 8,
    background: '#fff', color: '#111827',
    overflowY: 'auto',
    fontSize: 12, lineHeight: 1.65,
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
};
const footerStyle = {
    display: 'flex', justifyContent: 'flex-end', gap: 8,
    padding: '10px 14px', borderTop: '1px solid #e5e7eb',
    background: '#f9fafb',
};
const primaryBtnStyle = {
    border: '1px solid #4f46e5', background: '#4f46e5', color: '#fff',
    padding: '6px 12px', borderRadius: 6, cursor: 'pointer', fontSize: 13,
};
const secondaryBtnStyle = {
    border: '1px solid #d1d5db', background: '#fff', color: '#374151',
    padding: '6px 12px', borderRadius: 6, cursor: 'pointer', fontSize: 13,
};


// ──────────────────────────────────────────────────────────────────────
// Lightweight markdown renderer.
// Deliberately tiny (~80 LoC) to avoid pulling in `react-markdown` +
// `remark`/`rehype` (which would add ~80 KB gzipped).  Handles the
// subset the OCR pipeline actually emits:
//   ## Heading 2          → <h2>
//   ### Heading 3         → <h3>
//   ![image: ...]         → italic caption line
//   > quote               → blockquote
//   | a | b |             → table rows (with `---` separator detection)
//   blank line            → paragraph break
//   everything else       → paragraph text (wrapped, monospace-free)
// Anything we don't recognise renders as a paragraph — never as raw HTML
// — so untrusted OCR text can never inject markup.
// ──────────────────────────────────────────────────────────────────────
function renderMarkdown(src) {
    const lines = String(src || '').split(/\r?\n/);
    const out = [];
    let para = [];
    let table = null;       // { headers: string[], rows: string[][] } | null
    let inCode = false;
    let code = [];

    function flushPara() {
        if (!para.length) return;
        out.push(
            <p key={`p-${out.length}`} style={{ margin: '0 0 8px', whiteSpace: 'pre-wrap' }}>
                {para.join('\n')}
            </p>,
        );
        para = [];
    }
    function flushTable() {
        if (!table) return;
        out.push(
            <div key={`t-${out.length}`} style={{ overflowX: 'auto', margin: '6px 0 10px' }}>
                <table style={{ borderCollapse: 'collapse', fontSize: 11, width: '100%' }}>
                    {table.headers && (
                        <thead>
                            <tr>{table.headers.map((h, i) => (
                                <th key={i} style={tdStyle(true)}>{h}</th>
                            ))}</tr>
                        </thead>
                    )}
                    <tbody>
                        {table.rows.map((r, ri) => (
                            <tr key={ri}>{r.map((c, ci) => (
                                <td key={ci} style={tdStyle(false)}>{c}</td>
                            ))}</tr>
                        ))}
                    </tbody>
                </table>
            </div>,
        );
        table = null;
    }

    for (let i = 0; i < lines.length; i++) {
        const raw = lines[i];
        const line = raw.replace(/\s+$/, '');

        // Fenced code block (```) — preserve verbatim.
        if (/^```/.test(line)) {
            if (inCode) {
                out.push(
                    <pre key={`c-${out.length}`} style={preStyle}>
                        <code>{code.join('\n')}</code>
                    </pre>,
                );
                code = [];
                inCode = false;
            } else {
                flushPara(); flushTable();
                inCode = true;
            }
            continue;
        }
        if (inCode) { code.push(raw); continue; }

        // Markdown table row.
        if (/^\s*\|.*\|\s*$/.test(line)) {
            flushPara();
            const cells = line.trim().slice(1, -1).split('|').map(c => c.trim());
            // Header separator row (| --- | --- |) — promote previous row to headers.
            if (cells.every(c => /^:?-+:?$/.test(c)) && table && table.rows.length) {
                table.headers = table.rows.pop();
                continue;
            }
            if (!table) table = { headers: null, rows: [] };
            table.rows.push(cells);
            continue;
        }
        if (table) flushTable();

        if (/^\s*$/.test(line)) { flushPara(); continue; }

        // Headings.
        const h = line.match(/^(#{1,4})\s+(.*)$/);
        if (h) {
            flushPara();
            const level = h[1].length;
            const Tag = `h${Math.min(level + 1, 6)}`;     // ## → h3, ### → h4 (smaller)
            out.push(
                <Tag key={`h-${out.length}`} style={headingStyle(level)}>
                    {h[2]}
                </Tag>,
            );
            continue;
        }

        // Blockquote.
        if (/^>\s?/.test(line)) {
            flushPara();
            out.push(
                <blockquote key={`q-${out.length}`} style={quoteStyle}>
                    {line.replace(/^>\s?/, '')}
                </blockquote>,
            );
            continue;
        }

        // Image-caption line emitted by the OCR pipeline.
        if (/^!\[image:.*\]/.test(line)) {
            flushPara();
            out.push(
                <div key={`img-${out.length}`} style={imageCaptionStyle}>
                    {line.replace(/^!\[/, '').replace(/\]$/, '')}
                </div>,
            );
            continue;
        }

        para.push(line);
    }
    flushPara(); flushTable();
    return out;
}

function headingStyle(level) {
    const sizes = { 1: 16, 2: 14, 3: 13, 4: 12 };
    return {
        margin: level <= 2 ? '14px 0 6px' : '10px 0 4px',
        fontSize: sizes[level] || 12,
        fontWeight: 700,
        color: '#111827',
        borderBottom: level === 1 || level === 2 ? '1px solid #e5e7eb' : 'none',
        paddingBottom: level <= 2 ? 4 : 0,
    };
}
function tdStyle(isHeader) {
    return {
        border: '1px solid #e5e7eb',
        padding: '4px 8px',
        textAlign: 'left',
        background: isHeader ? '#f3f4f6' : '#fff',
        fontWeight: isHeader ? 600 : 400,
        verticalAlign: 'top',
    };
}
const preStyle = {
    background: '#f9fafb', border: '1px solid #e5e7eb',
    borderRadius: 6, padding: 10, fontSize: 11,
    overflowX: 'auto', margin: '6px 0 10px',
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
};
const quoteStyle = {
    margin: '6px 0', padding: '4px 10px',
    borderLeft: '3px solid #c7d2fe', background: '#eef2ff',
    color: '#3730a3', fontStyle: 'italic', fontSize: 11,
};
const imageCaptionStyle = {
    margin: '8px 0 4px', padding: '4px 8px',
    background: '#fdf4ff', border: '1px solid #f5d0fe',
    borderRadius: 4, color: '#86198f',
    fontSize: 11, fontStyle: 'italic',
};
