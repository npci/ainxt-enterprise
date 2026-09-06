// SPDX-License-Identifier: MIT
import { useState, useRef, useEffect, useCallback, useMemo, memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import useWorkflowStore from '../../../store/workflowStore';
import useDashboardStore from '../../../store/dashboardStore';
import SubagentCounterChip from '../../_shared/SubagentCounterChip';
import { selectActiveSubagents, selectAllSubagents } from './subagentSelectors';
import useTriggersStore from '../../../store/triggersStore';
import { API_BASE, buildAuthHeaders, kbFetch } from '../../../config/api';
import { stripEmoji } from '../../../utils/stripEmoji';
import { makeId } from '../../../utils/makeId';
import { downloadGeneratedFile } from '../../_shared/downloadGeneratedFile';
import { sniffGeneratedFiles, stripBareGeneratedPaths, stripGeneratedMarkdownLinks, PRIMARY_DOWNLOAD_EXTS } from '../../_shared/sniffGeneratedFiles';
import { useGeneratedDownload } from '../../_shared/useGeneratedDownload';
import DownloadNotice from '../../_shared/DownloadNotice';
import RunSettingsStrip from './RunSettingsStrip';
import DebugLogView from './DebugLogView';
import { useShareActions } from '../../_shared/useShareActions';
import ExtractedTextPreview from '../../_shared/ExtractedTextPreview';
import { sanitizeUserMessageForDisplay } from '../../../utils/threadHelpers';
import {
    loadActiveThread,
    saveActiveThread,
    loadComposerDraft,
    saveComposerDraft,
} from '../../../utils/editorPersistence';

// Build Studio chat-pane attachment limits. Workflow Builder uses the same
// `/agent-runner/attachment` parser path as Agent Builder, so both clip icons
// expose the same document formats and prompt budget. Image extensions are
// included so the OCR pipeline can handle standalone screenshots/photos.
const CHAT_ATTACH_ACCEPT = [
    '.pdf', '.docx', '.pptx', '.xlsx', '.xls', '.csv',
    '.html', '.htm', '.rtf', '.txt', '.json', '.md',
    '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp',
].join(',');
const CHAT_ATTACH_MAX_FILES = 5;
const CHAT_ATTACH_PROMPT_BUDGET_CHARS = 60000; // matches /agent-runner/attachment cap

// Image formats are routed to /agent-runner/image-asset (saved as sandbox
// assets the agent can reference by path) instead of /agent-runner/attachment
// (which OCRs/extracts text and fails on logos with no readable text).
const IMAGE_ASSET_EXTS = new Set(['png', 'jpg', 'jpeg', 'tiff', 'tif', 'bmp', 'webp']);
const isImageAsset = (filename) => IMAGE_ASSET_EXTS.has((filename.split('.').pop() || '').toLowerCase());

// Visual styling keyed by attachment status, hoisted so the chip render
// doesn't evaluate three parallel ternaries per style prop on every render.
const ATTACH_CHIP_STYLE = {
    uploading: { border: '#e5e7eb', bg: '#f9fafb', fg: '#6b7280', icon: '⟳' },
    ready:     { border: '#c7d2fe', bg: '#eef2ff', fg: '#3730a3', icon: '📎' },
    error:     { border: '#fecaca', bg: '#fef2f2', fg: '#b91c1c', icon: '×' },
};

// Hoisted so the chip label-truncate style keeps referential equality across
// renders inside the attachments .map() — otherwise a fresh object on every
// render defeats memoisation of the inner span.
const ATTACH_CHIP_LABEL_STYLE = { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' };

// Stable, collision-free id — `crypto.randomUUID` is already used by
// sibling components for the same purpose. Hoisted so the closure isn't
// rebuilt on every file pick.
function _newAttachId() {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
        return crypto.randomUUID();
    }
    return `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function _formatFileSize(bytes) {
    if (!bytes && bytes !== 0) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Safely convert any value to string — handles objects/JSON returned by LLMs
function safeString(value) {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    if (typeof value === 'object') return JSON.stringify(value, null, 2);
    return String(value);
}

// Pull a human-readable string out of a backend error body or SSE error
// frame whose `detail` may be a plain string OR a structured {code, message}
// object (e.g. budget errors: {"code":"BUDGET_EXCEEDED","message":"..."}).
// Without this the object gets stringified to "[object Object]" or a raw
// JSON blob in the error card.
function errText(detail, message, fallback) {
    if (typeof detail === 'string' && detail) return detail;
    if (detail && typeof detail === 'object') return detail.message || detail.code || fallback;
    if (typeof message === 'string' && message) return message;
    return fallback;
}

function formatUsageValue(value) {
    const num = Number(value || 0);
    if (!Number.isFinite(num)) return '0';
    return Math.round(num).toLocaleString();
}

function formatCostUsd(value) {
    const num = Number(value || 0);
    if (!Number.isFinite(num) || num <= 0) return '$0.000000';
    return `$${num.toFixed(6)}`;
}

function usageSummaryText(usage) {
    if (!usage) return '';
    const total = Number(usage.total_tokens || 0);
    const prompt = Number(usage.prompt_tokens || 0);
    const completion = Number(usage.completion_tokens || 0);
    if (!total && !prompt && !completion && !Number(usage.cost_usd || 0)) return '';
    const estimated = usage.estimated ? ' est.' : '';
    return `${formatUsageValue(total || (prompt + completion))} tokens${estimated} · ${formatCostUsd(usage.cost_usd)}`;
}

// Electron's renderer doesn't expose navigator.clipboard, so we fall back
// to a hidden textarea + execCommand. Shared by CodeBlock and ToolCallDetails.
function copyTextToClipboard(text) {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.position = 'fixed';
    textArea.style.left = '-9999px';
    document.body.appendChild(textArea);
    textArea.select();
    let ok = false;
    try {
        ok = document.execCommand('copy');
    } catch (err) {
        console.error('Failed to copy:', err);
    }
    document.body.removeChild(textArea);
    return ok;
}

// Custom code block component with copy button
function CodeBlock({ children, className }) {
    const [copied, setCopied] = useState(false);
    const codeContent = String(children).replace(/\n$/, '');

    // Only show as code block if it's truly multi-line code
    const isMultiLine = codeContent.includes('\n');

    const handleCopy = () => {
        if (copyTextToClipboard(codeContent)) {
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        }
    };

    // For single-line code, render as inline code style
    if (!isMultiLine) {
        return <code className="inline-code">{codeContent}</code>;
    }

    return (
        <div className="code-block-wrapper">
            <button className="code-copy-btn" onClick={handleCopy} title="Copy code">
                {copied ? (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <polyline points="20 6 9 17 4 12"></polyline>
                    </svg>
                ) : (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                    </svg>
                )}
            </button>
            <pre className={className}>
                <code>{codeContent}</code>
            </pre>
        </div>
    );
}

// Module-level so the inline `style` prop on the send button's SVG keeps
// referential equality across renders — otherwise React diffs and reapplies
// the style on every keystroke while the user types.
const SVG_NO_POINTER = { pointerEvents: 'none' };

// Markdown components configuration
// Map common extensions to a short kind label shown beneath the filename
// in the download card. Kept here (not in a utils file) because it's only
// used by the FileDownloadCard renderer below.
const FILE_KIND_LABELS = {
    pptx: 'PowerPoint',
    ppt:  'PowerPoint',
    docx: 'Word document',
    doc:  'Word document',
    xlsx: 'Excel spreadsheet',
    xls:  'Excel spreadsheet',
    pdf:  'PDF',
    csv:  'CSV',
    txt:  'Text',
    md:   'Markdown',
    json: 'JSON',
    zip:  'Archive',
};

// Auth-less fallback: when no `onDownload` is supplied (the module-level
// `markdownComponents` used by the HITL prompt has no component context to
// surface a toast), we STILL must not let the browser natively navigate to
// the raw file URL — that request carries no auth header and would 401 /
// show raw JSON / redirect. Route through the auth'd helper instead.
async function fallbackDownload({ href, filename }) {
    await downloadGeneratedFile(href, filename);
}

// `onDownload` opt-in handles 410 (file already consumed). When omitted we
// use `fallbackDownload` rather than native navigation.
function FileDownloadCard({ href, filename, label, onDownload, busy = false }) {
    const ext = (filename.split('.').pop() || '').toLowerCase();
    const kind = FILE_KIND_LABELS[ext] || (ext ? `${ext.toUpperCase()} file` : 'File');
    const handleClick = (e) => {
        e.preventDefault();
        if (busy) return; // ignore repeat clicks while a download is in flight
        (onDownload || fallbackDownload)({ href, filename });
    };
    return (
        // href + download retained for middle-click / Ctrl-click / a11y, but
        // a plain left-click is always intercepted so the request is auth'd.
        <a
            href={href}
            rel="noopener noreferrer"
            className={`file-download-card${busy ? ' is-downloading' : ''}`}
            download={filename}
            aria-busy={busy}
            onClick={handleClick}
        >
            <span className="file-download-card-icon" aria-hidden="true">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <polyline points="14 2 14 8 20 8" />
                </svg>
            </span>
            <div className="file-download-card-body">
                <span className="file-download-card-name">{label || filename}</span>
                <span className="file-download-card-meta">{kind}</span>
            </div>
            <span className="file-download-card-btn">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <polyline points="7 10 12 15 17 10" />
                    <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                {busy ? 'Preparing…' : 'Download'}
            </span>
        </a>
    );
}

// Extract a usable filename from either the href tail or the link text.
function extractFilename(href, childText) {
    const tail = (href || '').split('/').filter(Boolean).pop() || '';
    if (tail.includes('.')) return decodeURIComponent(tail);
    if (childText && childText.includes('.')) return childText;
    return tail || childText || 'file';
}

// Markdown link/image targets come from streamed agent text, which can
// itself echo attacker-supplied chat input (prompt injection). ReactMarkdown
// escapes text nodes but happily passes an `href`/`src` straight through to
// the DOM. A crafted `[click me](javascript:...)` or `[x](data:text/html,...)`
// would otherwise render as a live, clickable/loadable URI — a reflected XSS
// sink (CWE-79). Allow-list safe schemes and treat anything else (including
// unparsable strings) as an inert '#' link.
const SAFE_URL_SCHEMES = new Set(['http:', 'https:', 'mailto:', 'tel:']);

// ── URL path-segment allow-list (SSRF / path-injection guard) ───────────────
//
// The dynamic values interpolated into fetch() URL paths in this file
// (thread id, workflow id) are always internal identifiers — never a full
// URL, host, or scheme. This positive allow-list regex is a full-string
// match: the value must be composed ENTIRELY of [a-zA-Z0-9_-] characters and
// be 1-100 of them.
//
// Unlike a strip-and-continue approach (`.replace(/[^...]/g, '')`), a value
// that fails this test is REJECTED outright — the request is never sent, not
// even with a mangled/stripped value. Every character used to alter a URL's
// structure (`/`, `:`, `.`, `\`, `?`, `#`, `@`, whitespace) is outside the
// allowed set, so no such value can ever reach fetch(), regardless of where
// it originated.
const SAFE_PATH_SEGMENT_RE = /^[a-zA-Z0-9_-]{1,100}$/;

function sanitizeMarkdownHref(href) {
    if (typeof href !== 'string' || !href) return '#';
    const trimmed = href.trim();
    // Scheme-relative, root-relative, fragment, and query links have no
    // scheme to abuse and are safe to pass through as-is.
    if (/^(#|\?|\/(?!\/))/.test(trimmed)) return trimmed;
    try {
        // Resolve against a fixed dummy origin so protocol-relative URLs
        // (`//evil.com`) and relative paths parse without throwing, then
        // read back the actual scheme the browser would use.
        const url = new URL(trimmed, 'https://sanitize.invalid/');
        return SAFE_URL_SCHEMES.has(url.protocol) ? trimmed : '#';
    } catch {
        return '#';
    }
}

// Real gate (not a rename) between the fetch() response and the SSE reader.
// Rejects anything that isn't an actual ReadableStream before a single byte
// is read, and normalizes to `null` on any shape we don't recognize so
// callers have one explicit failure path instead of trusting the object
// handed back by fetch(). This is a genuine validation step — it changes
// control flow — rather than the earlier `_validatedBody` alias, which was
// a rename with no check attached (CWE-79 source→sink hardening).
function getValidatedStreamBody(response) {
    const body = response && response.body;
    if (!body || typeof body.getReader !== 'function') return null;
    return body;
}

// Every SSE `agent_token` chunk is untrusted server/model output that ends
// up in the chat markdown pane. Plain text is already escaped by React /
// ReactMarkdown, but control characters (e.g. a stray U+0000 or ANSI
// escapes some terminals/renderers interpret) have no legitimate place in
// chat text, so we strip them here — a real, content-changing sanitizer
// applied at the point untrusted data enters app state, not just at the
// final render.
function sanitizeStreamToken(token) {
    const text = String(token ?? '');
    // eslint-disable-next-line no-control-regex -- intentional: strip C0/C1 control chars.
    return text.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/g, '');
}

// Factory that builds a ReactMarkdown components map aware of any
// `generatedFiles` attached to the message. Inline code spans and links
// that name a generated artifact are routed through `onDownload` so a
// bare-filename href (e.g. `[Download X](X)`) doesn't get caught by the
// SPA router. Rendered as plain inline anchors — the chip strip below
// the message body is the canonical download UX.
function buildMarkdownComponents(generatedFiles, onDownload, excludeNames) {
    const filesByName = new Map();
    const excluded = new Set();
    if (excludeNames) {
        for (const name of excludeNames) {
            if (typeof name === 'string' && name) excluded.add(name.toLowerCase());
        }
    }
    for (const f of (generatedFiles || [])) {
        if (!f) continue;
        // Index under every name the LLM might cite: the human-readable
        // filename, the on-disk name (run-id prefixed), and the URL tail.
        // Without this, a markdown link like `[foo.pptx](foo.pptx)` slips
        // through to the browser as a relative URL and 404s.
        if (f.filename) filesByName.set(f.filename, f);
        if (f.disk_name) filesByName.set(f.disk_name, f);
        if (f.download_url) {
            const tail = f.download_url.split('/').filter(Boolean).pop();
            if (tail) filesByName.set(decodeURIComponent(tail), f);
        }
    }
    // Even when onDownload is omitted (the module-level default used for
    // HITL), never fall through to a native navigation — that request has no
    // auth header. Route through the auth'd fallback helper instead.
    const renderDownloadAnchor = (artifact, children, props) => (
        <a
            href={artifact.href}
            download={artifact.filename}
            onClick={(e) => { e.preventDefault(); (onDownload || fallbackDownload)(artifact); }}
            {...props}
        >
            {children}
        </a>
    );
    return {
        code({ node, inline, className, children, ...props }) {
            if (inline) {
                const text = (Array.isArray(children) ? children.join('') : String(children || '')).trim();
                const match = filesByName.get(text);
                if (match && match.download_url) {
                    return renderDownloadAnchor(
                        { href: `${API_BASE}${match.download_url}`, filename: match.filename },
                        text,
                    );
                }
                return <code className="inline-code" {...props}>{children}</code>;
            }
            return <CodeBlock className={className}>{children}</CodeBlock>;
        },
        a({ href, children, ...props }) {
            const isGeneratedHref = href && href.startsWith('/generated-files/');
            if (!isGeneratedHref && filesByName.size === 0) {
                return <a href={sanitizeMarkdownHref(href)} target="_blank" rel="noopener noreferrer" {...props}>{children}</a>;
            }
            const childText = (Array.isArray(children) ? children.join('') : String(children || '')).trim();
            if (isGeneratedHref) {
                const filename = extractFilename(href, childText);
                if (excluded.has(filename.toLowerCase())) {
                    return <span {...props}>{children}</span>;
                }
                return renderDownloadAnchor(
                    { href: `${API_BASE}${href}`, filename },
                    children,
                    props,
                );
            }
            const candidate = filesByName.get(extractFilename(href, childText)) || filesByName.get(childText);
            if (candidate && candidate.download_url) {
                return renderDownloadAnchor(
                    { href: `${API_BASE}${candidate.download_url}`, filename: candidate.filename },
                    children,
                    props,
                );
            }
            return <a href={sanitizeMarkdownHref(href)} target="_blank" rel="noopener noreferrer" {...props}>{children}</a>;
        },
    };
}

// Backwards-compatible default (no generated-file awareness) for
// surfaces that don't carry generated files — currently the HITL prompt.
const markdownComponents = buildMarkdownComponents([]);

// Module-level constant so it isn't reallocated each render. Enables GFM
// tables, strikethrough, task lists, and autolinks — without it, agent
// replies that contain markdown tables render as walls of pipe characters.
const markdownRemarkPlugins = [remarkGfm];


const WORKFLOW_PREVIEW_PROMPTS = [
    'Test workflow',
    'Run sample input',
    'Simulate agent response',
];

function createThreadId(workflowId) {
    const base = workflowId || 'workflow';
    return `${base}:${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

// Extract the attached-filenames + typed-question out of the persisted
// user prompt shape ("[File: x]\n<parsed>\n\n[File: y]\n<parsed>\n\n
// User question: <text>"). Live sends store the typed text in
// `msg.content` and the file metadata in `msg.attachments` — history
// replay must reproduce the same shape or the bubble will dump the
// raw OCR text into the message body (which is what users saw pre-fix).
//
// Returns `{ text, filenames }`. If the persisted content doesn't match
// the file-prefix shape at all, `text` is the untouched original and
// `filenames` is empty — never lossy.
function parsePersistedUserPrompt(rawContent) {
    if (typeof rawContent !== 'string' || !rawContent.startsWith('[File: ')) {
        return { text: rawContent || '', filenames: [] };
    }
    const filenames = [];
    let cursor = 0;
    while (rawContent.startsWith('[File: ', cursor)) {
        const close = rawContent.indexOf(']', cursor + 7);
        if (close === -1) break;
        filenames.push(rawContent.slice(cursor + 7, close));
        const nextFile = rawContent.indexOf('\n\n[File: ', close);
        const userQ    = rawContent.indexOf('\n\nUser question: ', close);
        if (userQ !== -1 && (nextFile === -1 || userQ < nextFile)) {
            cursor = userQ + '\n\nUser question: '.length;
            break;
        }
        if (nextFile === -1) {
            // Malformed prefix — bail out and keep the raw content so we
            // never silently drop data.
            return { text: rawContent, filenames: [] };
        }
        cursor = nextFile + 2;
    }
    if (filenames.length === 0) return { text: rawContent, filenames: [] };
    return { text: rawContent.slice(cursor).trim(), filenames };
}

function mapHistoryToUiMessages(historyMessages) {
    // Skip any persisted user message whose content is empty/whitespace —
    // older builds occasionally saved an empty user ChatMessage on HITL
    // resume and subflow completion paths, which would otherwise render
    // as an empty user bubble on reload. Assistant messages with empty
    // content are kept so download chips on `generated_files` still
    // render even when the assistant produced no prose.
    return (historyMessages || []).reduce((acc, msg) => {
        const isAssistant = msg.role === 'assistant';
        const rawContent = safeString(msg.content);
        if (!isAssistant && !rawContent.trim()) return acc;

        let displayContent = rawContent;
        let attachments = null;
        if (!isAssistant) {
            // Strip the "[File: ...]\n<parsed>\n\nUser question: ..."
            // wrapper so the reloaded bubble shows the typed question
            // + a compact attachment chip, matching the live send. The
            // full parsed dump stays in the backend history for the LLM.
            const parsed = parsePersistedUserPrompt(rawContent);
            if (parsed.filenames.length > 0) {
                displayContent = parsed.text;
                attachments = parsed.filenames.map((name) => ({
                    file_name: name,
                    // file_type / file_size aren't in the persisted prompt
                    // — the chip renderer already treats them as optional.
                }));
            } else {
                // Fallback shape (Attached document "x":\n---\n...):
                // reuse the shared sanitizer so any future prefix shape
                // added to threadHelpers is picked up here for free.
                displayContent = sanitizeUserMessageForDisplay(rawContent);
            }
        }

        const ui = {
            type: isAssistant ? 'assistant' : 'user',
            content: displayContent,
        };
        if (attachments) ui.attachments = attachments;
        // Restore download chips on reload — the backend persists
        // generated_files alongside the assistant message so the
        // FileDownloadCard strip re-renders without a re-run.
        if (Array.isArray(msg.generated_files) && msg.generated_files.length > 0) {
            ui.generatedFiles = msg.generated_files;
        }
        // Restore duration chip after page reload / thread switch.
        if (isAssistant && msg.duration_s != null) ui.durationS = msg.duration_s;
        acc.push(ui);
        return acc;
    }, []);
}

// Strip a leading "[File: name]...\n\nUser question: <text>" marker from a
// persisted title/preview so the history sidebar shows the user's typed text,
// not the internal attachment marker we now persist into user_input (see the
// send path). Falls back to the raw string when there's no marker.
function cleanThreadText(raw) {
    if (typeof raw !== 'string' || !raw) return raw || '';
    const parsed = parsePersistedUserPrompt(raw);
    if (parsed.filenames.length > 0) {
        // Prefer the typed question; if the user attached with no text, show a
        // compact "(file attached)" hint instead of an empty title.
        return parsed.text || `(${parsed.filenames.length} file${parsed.filenames.length === 1 ? '' : 's'} attached)`;
    }
    return raw;
}

function threadTitle(thread) {
    return cleanThreadText(thread.title) || 'New chat';
}

function threadPreview(thread) {
    return cleanThreadText(thread.last_message_preview) || 'Start testing this workflow';
}

function formatRelativeTime(isoTs) {
    if (!isoTs) return '';
    const now = Date.now();
    const then = Date.parse(isoTs);
    if (Number.isNaN(then)) return '';
    const deltaMs = Math.max(0, now - then);
    const mins = Math.floor(deltaMs / 60000);
    if (mins < 1) return 'now';
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h`;
    const days = Math.floor(hrs / 24);
    if (days < 7) return `${days}d`;
    const weeks = Math.floor(days / 7);
    return `${weeks}w`;
}

function getThreadGroup(thread) {
    const ts = Date.parse(thread.last_updated || '');
    if (Number.isNaN(ts)) return 'Older';

    const now = new Date();
    const date = new Date(ts);
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const startOfThreadDay = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
    const dayDiff = Math.floor((startOfToday - startOfThreadDay) / 86400000);

    if (dayDiff <= 0) return 'Today';
    if (dayDiff === 1) return 'Yesterday';
    if (dayDiff <= 7) return 'Last 7 Days';
    return 'Older';
}

function groupThreads(threadsToGroup) {
    const groups = {
        Today: [],
        Yesterday: [],
        'Last 7 Days': [],
        Older: [],
    };

    threadsToGroup.forEach((thread) => {
        groups[getThreadGroup(thread)].push(thread);
    });

    return Object.entries(groups).filter(([, items]) => items.length > 0);
}

function getThinkingStage(streamingAgent, streamingContent) {
    if (streamingContent) return 'Streaming response';
    if (streamingAgent?.includes('->')) return 'Searching context';
    if (streamingAgent) return 'Generating response';
    return 'Understanding request';
}

// streamingAgent comes through as one of:
//   "<agent name>"                 (agent is generating)
//   "<agent name> -> <tool>"       (agent is calling a tool)
//   "<agent name> working..."      (post-tool, back to generating)
// Pull out just the agent so we can label the thinking bubble cleanly.
function parseStreamingAgent(streamingAgent) {
    if (!streamingAgent) return '';
    const beforeArrow = streamingAgent.split('->')[0];
    return beforeArrow.replace(/working\.\.\.$/i, '').trim();
}

// Inline SVG icons for the timeline. Kept as constants so the JSX stays
// readable and React doesn't recreate the elements on every render.
const TIMELINE_CHECK_ICON = (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="20 6 9 17 4 12" />
    </svg>
);

const TIMELINE_TOOL_ICON = (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
);

// Chevron for the collapsible sub-agent rows. Mirrors the SVG used by
// SubagentCounterChip so the two accordions look identical.
const TIMELINE_CHEVRON_ICON = (
    <svg width="10" height="10" viewBox="0 0 16 16" aria-hidden="true">
        <path d="M5.5 3.5 L10.5 8 L5.5 12.5" fill="none"
              stroke="currentColor" strokeWidth="1.6"
              strokeLinecap="round" strokeLinejoin="round" />
    </svg>
);

const TIMELINE_SKILL_ICON = (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
        <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    </svg>
);

// Collapsible sub-agent timeline row. Default collapsed: a chevron + bold
// alias + live status. Expanding reveals the task, the tools/skills the
// worker is using, and (on completion/failure) the result/error. Lives as
// its own memoised component so each running row can own a 1-second
// elapsed-time interval without re-rendering the whole timeline.
const SubagentTimelineStep = memo(function SubagentTimelineStep({ step }) {
    const [open, setOpen] = useState(false);
    const [nowTick, setNowTick] = useState(() => Date.now());

    const isRunning = step.status === 'running';

    // Tick once per second ONLY while running, so the elapsed label updates
    // live. Cleared the moment the row stops running (or unmounts).
    useEffect(() => {
        if (!isRunning) return undefined;
        const id = setInterval(() => setNowTick(Date.now()), 1000);
        return () => clearInterval(id);
    }, [isRunning]);

    const toggle = useCallback(() => setOpen((v) => !v), []);

    const tools  = Array.isArray(step.tools)  ? step.tools  : [];
    const skills = Array.isArray(step.skills) ? step.skills : [];
    const files  = Array.isArray(step.files)  ? step.files  : [];

    // Status label, with a ticking timer while running.
    let stateLabel;
    if (step.status === 'planning') {
        stateLabel = 'planning';
    } else if (step.status === 'complete') {
        stateLabel = typeof step.durationS === 'number' ? `${step.durationS}s` : 'done';
    } else if (step.status === 'failed') {
        stateLabel = 'failed';
    } else {
        const elapsed = step.startedAt
            ? Math.max(0, Math.round((nowTick - step.startedAt) / 1000))
            : null;
        stateLabel = elapsed != null ? `running · ${elapsed}s` : 'running';
    }

    const marker = step.status === 'complete' ? TIMELINE_CHECK_ICON
        : step.status === 'failed' ? '⚠'
        : <span className="thinking-step-dot" />;

    // A row is worth expanding only when it carries detail to show.
    const hasDetail = Boolean(
        step.taskPreview || tools.length || skills.length || files.length
        || (step.status === 'failed' && step.error)
        || (step.status === 'complete' && step.preview),
    );

    return (
        <li className={`thinking-step thinking-step--${step.status} thinking-step--subagent`}>
            <span className="thinking-step-marker" aria-hidden="true">{marker}</span>
            <div className="thinking-step-body">
                <button
                    type="button"
                    className={`thinking-subagent-rowtoggle ${open ? 'is-open' : ''}`}
                    onClick={hasDetail ? toggle : undefined}
                    aria-expanded={hasDetail ? open : undefined}
                    disabled={!hasDetail}
                >
                    {hasDetail && (
                        <span className={`thinking-subagent-row-chevron ${open ? 'is-open' : ''}`}
                              aria-hidden="true">
                            {TIMELINE_CHEVRON_ICON}
                        </span>
                    )}
                    <span className="thinking-step-agent">
                        <strong>{step.alias}</strong>
                        <span className="thinking-subagent-state"> · {stateLabel}</span>
                    </span>
                </button>

                {open && hasDetail && (
                    <div className="thinking-subagent-row-body">
                        {step.taskPreview && (
                            <div className="thinking-subagent-row-field">
                                <span className="thinking-subagent-row-field-label">Task</span>
                                <span className="thinking-subagent-row-field-value">{step.taskPreview}</span>
                            </div>
                        )}
                        {tools.length > 0 && (
                            <div className="thinking-subagent-row-field">
                                <span className="thinking-subagent-row-field-label">Tools</span>
                                <div className="thinking-step-tools">
                                    {tools.map((t, ti) => (
                                        <span key={`${step.callId}-tool-${ti}`}
                                              className="thinking-tool-chip thinking-tool-chip--tool"
                                              title={String(t)}>
                                            {TIMELINE_TOOL_ICON}
                                            <span className="thinking-tool-chip-name">{String(t)}</span>
                                        </span>
                                    ))}
                                </div>
                            </div>
                        )}
                        {skills.length > 0 && (
                            <div className="thinking-subagent-row-field">
                                <span className="thinking-subagent-row-field-label">Skills</span>
                                <div className="thinking-step-tools">
                                    {skills.map((s, si) => (
                                        <span key={`${step.callId}-skill-${si}`}
                                              className="thinking-tool-chip thinking-tool-chip--skill"
                                              title={String(s)}>
                                            {TIMELINE_SKILL_ICON}
                                            <span className="thinking-tool-chip-name">{String(s)}</span>
                                        </span>
                                    ))}
                                </div>
                            </div>
                        )}
                        {step.status === 'failed' && step.error && (
                            <div className="thinking-subagent-row-field thinking-subagent-row-field--error">
                                <span className="thinking-subagent-row-field-label">Error</span>
                                <span className="thinking-subagent-row-field-value">{step.error}</span>
                            </div>
                        )}
                        {step.status === 'complete' && step.preview && (
                            <div className="thinking-subagent-row-field">
                                <span className="thinking-subagent-row-field-label">Result</span>
                                <span className="thinking-subagent-row-field-value">{step.preview}</span>
                            </div>
                        )}
                        {files.length > 0 && (
                            <div className="thinking-step-tools">
                                {files.map((f, fi) => (
                                    <span key={`${step.callId}-f-${fi}`}
                                          className="thinking-tool-chip thinking-tool-chip--complete"
                                          title={f.filename || f.download_url}>
                                        📎 <span className="thinking-tool-chip-name">{f.filename || 'file'}</span>
                                    </span>
                                ))}
                            </div>
                        )}
                    </div>
                )}
            </div>
        </li>
    );
});

// Live progress card shown inside the assistant bubble while a workflow
// is executing. Renders the timeline persistently — even after the final
// agent starts streaming — so users keep seeing what every agent did,
// not just the final response text. The skeleton lines hide as soon as
// real content begins streaming.
//
// Memoised because streamingContent updates on every token, but the
// timeline only cares about the boolean `hasStreamingContent`. Without
// memo the entire <ol> + every SVG re-renders on every keystroke from
// the model.
function renderRoundChip(step) {
    if (step.loopRound == null) return null;
    const base = step.loopTotal != null
        ? `round ${step.loopRound} of ${step.loopTotal}`
        : `round ${step.loopRound}`;
    // Surface the confidence score inline with the round number so users
    // can scan "round 1 · 65% → round 2 · 80%" without expanding the
    // detail pill below. The same score is also available in
    // renderConditionSnapshot; both render whenever the loop emits one.
    const rawScore = step.condition?.evalState?.score;
    const scoreLabel = (typeof rawScore === 'number' && Number.isFinite(rawScore))
        ? (rawScore >= 0 && rawScore <= 1
            ? ` · ${Math.round(rawScore * 100)}%`
            : ` · ${rawScore.toFixed(2)}`)
        : '';
    return <span className="thinking-step-round">{base}{scoreLabel}</span>;
}

function renderConditionSnapshot(condition) {
    if (!condition) return null;
    const stateBits = [];
    const changesBit = (() => {
        const c = condition.evalState && condition.evalState.changes;
        if (!c || typeof c !== 'string') return '';
        const trimmed = c.trim();
        if (!trimmed) return '';
        // Soft-cap so a verbose model doesn't blow the row width.
        return trimmed.length > 140 ? trimmed.slice(0, 137) + '…' : trimmed;
    })();
    // Promote the numeric score to a "Confidence Score: 0.42" pill — the
    // signal users actually look at. We deliberately do NOT iterate the
    // remaining evalState keys: resolve_routing_state flattens raw JSON
    // (title, current_input, text…) and dumping those creates the unreadable
    // "current_input = {...} · text = {...} · title = …" line shown in the
    // earlier UI. Score + changes are the only loop-relevant signals.
    const rawScore = condition.evalState && condition.evalState.score;
    const hasScore = typeof rawScore === 'number' && Number.isFinite(rawScore);
    // When the LLM judge ran, attach a "(judged)" suffix so users know
    // this number came from an independent evaluator rather than the
    // body agent's self-report. ``evaluation`` is populated only by the
    // new loop_evaluation reducer branch; legacy self-report still wins
    // when evaluation is absent.
    const evaluation = condition.evaluation || null;
    const judgeRan = !!(evaluation && evaluation.judged);
    if (hasScore) {
        // Use percentage form when the value is in [0..1] (the model's
        // self-rated confidence range from the loop continuation contract),
        // otherwise show the raw decimal.
        const pct = (rawScore >= 0 && rawScore <= 1)
            ? `${Math.round(rawScore * 100)}%`
            : rawScore.toFixed(2);
        const sourceTag = judgeRan ? ' (judged)' : '';
        stateBits.push(`Confidence Score: ${pct}${sourceTag}`);
    }
    const matched = (condition.caseResults || []).some((c) => c.matched);
    const stopDecision = condition.stopDecision || null;
    // When the controller produced a decision we use ITS verdict text so
    // the UI reflects WHY the loop stopped (threshold / converged /
    // regression / max_iter / continue). Falls back to the case-based
    // verdict for legacy / self-report-only runs.
    const verdict = stopDecision
        ? (stopDecision.stop
            ? `stop (${stopDecision.reason || 'condition met'})`
            : 'continue')
        : (condition.willContinue
            ? 'continue'
            : (matched ? 'stop (no case matched)' : 'stop'));
    return (
        <div className="thinking-step-condition">
            {stateBits.length > 0 && (
                <span className="thinking-step-condition-state">{stateBits.join(' · ')}</span>
            )}
            <span className={`thinking-step-condition-verdict thinking-step-condition-verdict--${condition.willContinue ? 'continue' : 'stop'}`}>
                → {verdict}
            </span>
            {changesBit && (
                // "What changed this round" — the human-readable summary the
                // agent supplies as `changes` in its trailing JSON line. Sits
                // on its own row below the verdict so a long sentence wraps
                // naturally instead of fighting the score pill for space.
                <div className="thinking-step-condition-changes">
                    <span className="thinking-step-condition-changes-label">What changed:</span>{' '}
                    {changesBit}
                </div>
            )}
            {/* Judge rubric breakdown — rendered only when the loop opted in
                to useLlmEvaluator and the judge actually produced scores.
                Each criterion shows its weighted score and one-line reasoning;
                the overall reasoning sits above as the "Why" line. Inline
                <details> keeps the chat thread compact by default — users
                expand it only when they want to audit the score. */}
            {/* Inline "Why this score?" pill when there's no expandable rubric.
                Three cases:
                  1. LLM evaluator was configured and is currently running:
                     show "LLM evaluator (verifying…)" so users don't briefly
                     see "self-reported by agent" during the judge call.
                  2. LLM evaluator was not configured: "self-reported by agent"
                     (the score they see IS the body agent's self-report).
                  3. LLM evaluator ran but produced no criteria (fallback path):
                     "self-reported by agent" (accurate — we substituted). */}
            {!evaluation && hasScore && (
                <div className="thinking-step-condition-rubric thinking-step-condition-rubric--inline">
                    <span className="thinking-step-condition-rubric-summary">
                        Why this score?{' '}
                        <span className="thinking-step-condition-rubric-judge">
                            {condition.evaluatorPending
                                ? 'LLM evaluator (verifying…)'
                                : 'self-reported by agent'}
                        </span>
                    </span>
                </div>
            )}
            {evaluation && Array.isArray(evaluation.criteria) && evaluation.criteria.length > 0 && (
                <details className="thinking-step-condition-rubric">
                    <summary className="thinking-step-condition-rubric-summary">
                        Why this score?{' '}
                        <span className="thinking-step-condition-rubric-judge">
                            {judgeRan ? 'LLM evaluator' : 'self-reported by agent'}
                        </span>
                    </summary>
                    {evaluation.reasoning && (
                        <div className="thinking-step-condition-rubric-overall">
                            {evaluation.reasoning}
                        </div>
                    )}
                    <table className="thinking-step-condition-rubric-table">
                        <thead>
                            <tr>
                                <th>Criterion</th>
                                <th>Score</th>
                                <th>Weight</th>
                                <th>Reasoning</th>
                            </tr>
                        </thead>
                        <tbody>
                            {evaluation.criteria.map((c, idx) => {
                                const s = (typeof c.score === 'number' && Number.isFinite(c.score))
                                    ? `${Math.round(c.score * 100)}%`
                                    : '—';
                                const w = (typeof c.weight === 'number' && Number.isFinite(c.weight))
                                    ? `${Math.round(c.weight * 100)}%`
                                    : '—';
                                return (
                                    <tr key={`${c.name}-${idx}`}>
                                        <td>{c.name}</td>
                                        <td>{s}</td>
                                        <td>{w}</td>
                                        <td>{c.reasoning || ''}</td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                    {stopDecision && stopDecision.message && (
                        <div className="thinking-step-condition-rubric-decision">
                            <span className="thinking-step-condition-changes-label">Decision:</span>{' '}
                            {stopDecision.message}
                        </div>
                    )}
                </details>
            )}
        </div>
    );
}

// Shared handler for the `agent_retry` SSE event. The selected model's
// stream failed to open on a transient error and is being retried before the
// fallback engages. Surfaces a TRANSIENT status line inside the "Workflow
// running" card (cleared when the run ends) + a permanent Debug Log row —
// so the user sees each attempt live during processing without cluttering the
// chat transcript. Shared between the run-stream and resume-stream dispatchers.
function handleRetryNotice(data, { setFallbackStatus, pushDebugRow }) {
    const agent = data.data?.agent;
    const model = data.data?.model || 'the selected model';
    const nextAttempt = data.data?.next_attempt;
    const maxAttempts = data.data?.max_attempts;
    const delay = data.data?.delay_s;
    const err = data.data?.error;
    const attemptLabel = (nextAttempt && maxAttempts)
        ? `attempt ${nextAttempt}/${maxAttempts}`
        : 'again';
    // Short line for the timeline row; full detail goes to the Debug Log.
    const lineText = `Retrying ${attemptLabel}${delay ? ` in ${delay}s` : ''}…`;
    const noticeText =
        `${model} did not respond${err ? ` (${err})` : ''} — retrying ${attemptLabel}`
        + `${delay ? ` in ${delay}s` : ''}…`;
    // Keyed by agent so the line renders inside that node's timeline row
    // (the row already shows the agent name, so we don't repeat it here).
    setFallbackStatus({ agent, text: lineText });
    pushDebugRow({
        ts: new Date().toISOString(),
        nodeId: data.data?.node_id || null,
        title: 'Model retry',
        detail: noticeText,
        status: 'warning',
        raw: data,
    });
}

// Shared handler for the `agent_fallback` SSE event. The selected model
// failed and the fallback (Sonnet 4.6) took over for this turn. Like the
// retry handler, this is surfaced as a TRANSIENT processing status (not a
// persistent chat bubble) plus a Debug Log row for audit — a fallback is a
// successful self-healing event, so it shouldn't linger in the transcript
// like an error would.
function handleFallbackNotice(data, { setFallbackStatus, pushDebugRow }) {
    const agent = data.data?.agent;
    const primaryModel = data.data?.primary_model || 'the selected model';
    const fallbackModel = data.data?.fallback_model || 'the fallback model';
    const reason = data.data?.reason || 'primary model unavailable';
    // Short line for the timeline row; full detail goes to the Debug Log.
    const lineText = `Switched to fallback model ${fallbackModel}`;
    const noticeText =
        `${primaryModel} unavailable (${reason}) — switching to fallback model ${fallbackModel}…`;
    // Keyed by agent so the line renders inside that node's timeline row
    // (the row already shows the agent name, so we don't repeat it here).
    setFallbackStatus({ agent, text: lineText });
    pushDebugRow({
        ts: new Date().toISOString(),
        nodeId: data.data?.node_id || null,
        title: 'Model fallback',
        detail: noticeText,
        status: 'warning',
        raw: data,
    });
}

// Shared dispatcher for the loop_iteration_summary / loop_final_summary
// / loop_evaluation SSE events. Returns true when handled so the caller
// can short-circuit its else-if chain. Lives next to buildLoopSummaryContent
// so both summary code paths sit together and stay in sync.
function handleLoopSummaryEvent(event, payload, { addExecutionLog, setMessages }) {
    if (event === 'loop_iteration_summary') {
        const { node_id, index, score, changes } = payload || {};
        if (node_id) {
            addExecutionLog({
                type: 'loop_iter_summary',
                nodeId: node_id,
                index,
                score: typeof score === 'number' ? score : null,
                changes: changes || null,
            });
        }
        return true;
    }
    // New (LLM-judge) event — emitted ONLY when the loop node has
    // `useLlmEvaluator: true`. Carries the rubric-driven confidence score,
    // per-criterion breakdown, judge reasoning, and the controller's stop
    // decision. We push it as a distinct log type so the reducer can merge
    // it into the same activeLoops entry as the (legacy) self-reported
    // score, overwriting the latter — the judge is the source of truth.
    if (event === 'loop_evaluation') {
        const { node_id, index, evaluation, decision } = payload || {};
        if (node_id) {
            addExecutionLog({
                type: 'loop_iter_eval',
                nodeId: node_id,
                index,
                evaluation: evaluation || null,
                decision: decision || null,
            });
        }
        return true;
    }
    if (event === 'loop_final_summary') {
        const summary = payload || {};
        const content = buildLoopSummaryContent(summary);
        if (content) {
            setMessages(prev => [...prev, {
                type: 'assistant',
                content,
                loopSummary: summary,
            }]);
        }
        return true;
    }
    return false;
}

// Shared by the live-stream and resume-stream SSE handlers so the
// post-loop chat bubble stays identical across both code paths.
//
// Renders a *structured* summary only: iteration count, score progression,
// and per-round changes. The raw loop output (e.g. a planner's full JSON
// blob) is intentionally omitted — users see the final artifact (download
// card) at the end of the workflow, not the intermediate buffer.
function buildLoopSummaryContent(summary) {
    if (!summary) return '';
    const iterations = Array.isArray(summary.iterations) ? summary.iterations : [];
    const total = iterations.length;
    // Build a list of markdown blocks (paragraphs + lists). Blocks are
    // joined with blank lines so remark-gfm renders them as separate
    // paragraphs / proper lists instead of collapsing everything into one
    // flowing paragraph. The previous implementation joined lines with
    // single \n which markdown treats as soft-break-within-paragraph,
    // producing the "Changes per round: 2. ... — 3. ..." run-on text the
    // user reported.
    const blocks = [];
    blocks.push(
        summary.max_iterations_hit
            ? `**Loop reached the max-iterations cap after ${total} round${total === 1 ? '' : 's'}. Returning the highest-scoring output.**`
            : `**Loop completed in ${total} round${total === 1 ? '' : 's'}.**`
    );

    const scored = iterations.filter((it) => typeof it.score === 'number' && Number.isFinite(it.score));
    if (scored.length > 0) {
        // Render as a percentage for human-friendly comparison; mirrors the
        // chip on the timeline so users see the same units in both places.
        const pct = (v) => (v >= 0 && v <= 1 ? `${Math.round(v * 100)}%` : v.toFixed(2));
        const arrow = scored.map((it) => pct(it.score)).join(' → ');
        const deltaLabel = (typeof summary.delta === 'number')
            ? ` (Δ ${summary.delta >= 0 ? '+' : ''}${summary.delta.toFixed(2)})`
            : '';
        blocks.push(`**Score progression:** ${arrow}${deltaLabel}`);
    }

    // Backend emits `it.index` as a 0-based counter (0, 1, 2…). Convert to
    // 1-based for display so the rendered list reads "Round 1, 2, 3…".
    // Show EVERY round — even those where the agent skipped the JSON
    // contract — with a placeholder for missing changes so users see the
    // full iteration count and can spot which rounds the agent misbehaved.
    // Each list item sits on its own line — GFM requirement for a proper
    // <ol>/<ul> render (not a soft-wrapped paragraph).
    const changeItems = iterations.map((it, i) => {
        const round = (typeof it.index === 'number') ? it.index + 1 : (i + 1);
        const changes = it.changes || '_(agent did not emit a change summary this round)_';
        return `- **Round ${round}:** ${changes}`;
    });
    if (changeItems.length > 0) {
        blocks.push('**Changes per round:**');
        blocks.push(changeItems.join('\n'));
    }

    // Two newlines between blocks → blank line in markdown → paragraph /
    // list boundary that GFM honours.
    return blocks.join('\n\n');
}

const ThinkingTimeline = memo(function ThinkingTimeline({ timeline, stage, hasStreamingContent, activeSubagents, allSubagents, fallbackStatus }) {
    const activeCount = Array.isArray(activeSubagents) ? activeSubagents.length : 0;
    const totalCount  = Array.isArray(allSubagents)    ? allSubagents.length    : 0;
    return (
        <div className="thinking-card" role="status" aria-live="polite">
            <div className="thinking-card-header">
                <span className="thinking-pulse" aria-hidden="true" />
                <span className="thinking-card-title">Workflow running</span>
                {totalCount > 0 && (
                    <SubagentCounterChip
                        count={activeCount}
                        workers={activeSubagents}
                        subagents={allSubagents}
                    />
                )}
                <span className="thinking-stage">{stage}</span>
            </div>
            {timeline.length > 0 && (
                <ol className="thinking-timeline">
                    {timeline.map((step, idx) => {
                        const stepKey = `${step.kind}-${step.agent || step.nodeId}-${idx}`;
                        if (step.kind === 'loop_done') {
                            const msg = step.maxHit
                                ? `Loop stopped at safety cap (${step.total} rounds)`
                                : `Loop finished after ${step.total} round${step.total === 1 ? '' : 's'}`;
                            return (
                                <li key={stepKey} className="thinking-step thinking-step--complete thinking-step--loop-done">
                                    <span className="thinking-step-marker" aria-hidden="true">{TIMELINE_CHECK_ICON}</span>
                                    <div className="thinking-step-body">
                                        <span className="thinking-step-agent">{msg}</span>
                                    </div>
                                </li>
                            );
                        }
                        if (step.kind === 'subagent') {
                            // Collapsible sub-agent row: a chevron expands to
                            // reveal the task, the tools/skills the worker is
                            // using, and (on completion/failure) the
                            // result/error. Default collapsed so a noisy
                            // planner failure doesn't splatter the panel. See
                            // SubagentTimelineStep above.
                            return (
                                <SubagentTimelineStep key={stepKey} step={step} />
                            );
                        }
                        // Agent step — may carry an inline loop round chip
                        // and condition snapshot so we don't spawn a
                        // separate row per iteration.
                        return (
                            <li key={stepKey} className={`thinking-step thinking-step--${step.status}${step.loopRound != null ? ' thinking-step--in-loop' : ''}`}>
                                <span className="thinking-step-marker" aria-hidden="true">
                                    {step.status === 'complete' ? TIMELINE_CHECK_ICON : <span className="thinking-step-dot" />}
                                </span>
                                <div className="thinking-step-body">
                                    <span className="thinking-step-agent">
                                        {step.agent}
                                        {renderRoundChip(step)}
                                    </span>
                                    {renderConditionSnapshot(step.condition)}
                                    {fallbackStatus && fallbackStatus.agent === step.agent && step.status === 'running' && (
                                        <div className="thinking-fallback-status">{fallbackStatus.text}</div>
                                    )}
                                    {step.tools && step.tools.length > 0 && (() => {
                                        // Delegation tool calls (delegate_to_<alias>) are surfaced
                                        // by their own subagent_start/_complete pill rendered as a
                                        // separate timeline step. Hiding the raw chip here avoids
                                        // showing the user the synthetic snake_case tool name.
                                        const visibleTools = step.tools.filter(
                                            (t) => !(t && typeof t.name === 'string' && t.name.startsWith('delegate_to_'))
                                        );
                                        if (visibleTools.length === 0) return null;
                                        return (
                                            <div className="thinking-step-tools">
                                                {visibleTools.map((tool, ti) => (
                                                    <span
                                                        key={`${tool.name}-${ti}`}
                                                        className={`thinking-tool-chip thinking-tool-chip--${tool.status}`}
                                                        title={`Tool: ${tool.name}`}
                                                    >
                                                        {TIMELINE_TOOL_ICON}
                                                        <span className="thinking-tool-chip-name">{tool.name}</span>
                                                    </span>
                                                ))}
                                            </div>
                                        );
                                    })()}
                                </div>
                            </li>
                        );
                    })}
                </ol>
            )}
            {!hasStreamingContent && (
                <div className="thinking-skeleton" aria-hidden="true">
                    <span className="thinking-skeleton-line" />
                    <span className="thinking-skeleton-line short" />
                </div>
            )}
        </div>
    );
});

// Insert a subagent step into the flat timeline so it sits IMMEDIATELY
// after the agent step that owns it. Ownership resolution:
//
//   1. If the subagent's ``nodeId`` matches an agent step's ``nodeId``,
//      insert right after the LAST such agent (so if the same node runs
//      twice, the pill attaches to the most recent invocation — which is
//      what the operator is watching).
//   2. If ``nodeId`` is unset (chat path or older SSE), fall back to
//      appending after the LAST agent step in the array (best-effort —
//      matches the pre-fix behaviour for legacy callers).
//   3. If there is no agent step yet, push at the end (the swarm can
//      technically fire before any node stamps its agent_start when the
//      chat entry point runs a swarm directly with no wrapping agent).
//
// This function is intentionally pure and mutates ``steps`` in place so
// the existing timeline rebuild logic stays a single-pass linear walk.
// Complexity is O(n) per call (findLastIndex + splice); the timeline is
// bounded by the number of workflow nodes × iterations, so this is well
// within budget even on long runs.
function _spliceSubagentStep(steps, subStep) {
    if (!steps || !subStep) return;
    const targetNodeId = subStep.nodeId || null;
    let anchor = -1;
    if (targetNodeId) {
        for (let i = steps.length - 1; i >= 0; i--) {
            const s = steps[i];
            if (s && s.kind === 'agent' && s.nodeId && s.nodeId === targetNodeId) {
                anchor = i;
                break;
            }
        }
    }
    if (anchor === -1) {
        // No matching node → attach after the most recent agent step
        // regardless of nodeId. This preserves prior behaviour for
        // frames that predate the node_id contract.
        for (let i = steps.length - 1; i >= 0; i--) {
            if (steps[i] && steps[i].kind === 'agent') { anchor = i; break; }
        }
    }
    if (anchor === -1) {
        steps.push(subStep);
        return;
    }
    // Insert AFTER the anchor agent step. If there are already subagent
    // steps sitting behind that agent (same nodeId), append after the
    // last such sibling so pills stay in emission order beneath their
    // parent — otherwise the newest pill would jump above older ones.
    let insertAt = anchor + 1;
    while (
        insertAt < steps.length &&
        steps[insertAt] &&
        steps[insertAt].kind === 'subagent' &&
        // Only skip past sibling subagents whose nodeId matches (or
        // whose nodeId is unset — legacy). A different-node subagent
        // means we've walked past the current parent's cluster.
        (!steps[insertAt].nodeId ||
         !targetNodeId ||
         steps[insertAt].nodeId === targetNodeId)
    ) {
        insertAt++;
    }
    steps.splice(insertAt, 0, subStep);
}

// Walk the per-run execution log to produce an ordered list of agent
// steps, each with the tools it called. The last step is "running" until
// the matching agent_complete arrives. Used by the thinking card so users
// see the full pipeline progress on sequential workflows, not just the
// agent that fired last.
//
// Assumes strict sequential execution (the workflow runtime currently
// emits one agent_start at a time, completed before the next agent_start).
// If a parallel-agent runtime is ever added, the single `current` pointer
// will need to become a stack/map keyed by agent name.
function buildAgentTimeline(executionLogs, streamingAgent) {
    const steps = [];
    let current = null;
    // Active loops keyed by node id. Each entry tracks the current round /
    // mode / latest condition snapshot so subsequent agent_start rows can be
    // stamped with that context inline — instead of pushing one "Loop —
    // round N" step per iteration which grows the timeline linearly.
    const activeLoops = new Map();
    // Most-recently-started loop wins for stamping agent rows when multiple
    // loops are somehow active. Cheap stack so popping on loop_done restores
    // the outer loop's context for nested setups.
    const loopStack = [];

    for (const log of executionLogs) {
        if (log.type === 'agent_start') {
            const innermost = loopStack.length > 0
                ? activeLoops.get(loopStack[loopStack.length - 1])
                : null;
            const innermostLoopId = loopStack.length > 0
                ? loopStack[loopStack.length - 1]
                : null;
            // Collapse repeated iterations of the same agent inside the same
            // loop into a single timeline row whose round counter updates in
            // place. Without this, each loop iteration appends a new row,
            // exploding the timeline vertically (see Loop Flow / Slide
            // builder which fires once per round).
            const existingLoopRow = innermostLoopId != null
                ? steps.findLast(s => (
                    s.kind === 'agent' &&
                    s.agent === log.agent &&
                    s.loopNodeId === innermostLoopId
                ))
                : null;
            if (existingLoopRow) {
                existingLoopRow.status = 'running';
                existingLoopRow.tools = [];
                existingLoopRow.loopRound = innermost ? (innermost.index ?? 0) + 1 : existingLoopRow.loopRound;
                existingLoopRow.loopTotal = innermost ? innermost.total : existingLoopRow.loopTotal;
                existingLoopRow.loopMode = innermost ? innermost.mode : existingLoopRow.loopMode;
                // Clear the stale condition on every new round so the chip
                // and "What changed" line don't carry over the previous
                // round's score / changes. The upcoming loop_iter_summary
                // and loop_condition_eval events will populate it fresh.
                existingLoopRow.condition = null;
                // Refresh nodeId in case the same agent is reused in a
                // fresh iteration with a different node context (rare
                // but the runtime allows it via subflow inlining).
                if (log.nodeId) existingLoopRow.nodeId = log.nodeId;
                current = existingLoopRow;
            } else {
                current = {
                    kind: 'agent',
                    agent: log.agent,
                    // Owning workflow-graph node. Used below to attach
                    // subagent pills to the right parent step even when
                    // several nodes are visible in the timeline.
                    nodeId: log.nodeId || null,
                    tools: [],
                    status: 'running',
                    loopNodeId: innermostLoopId,
                    loopRound: innermost ? (innermost.index ?? 0) + 1 : null,
                    loopTotal: innermost ? innermost.total : null,
                    loopMode:  innermost ? innermost.mode : null,
                    // Fresh condition per round — don't inherit the previous
                    // round's score. The upcoming loop_iter_summary and
                    // loop_condition_eval events will populate it once THIS
                    // round's body finishes. Inheriting causes the chip to
                    // show a stale score (e.g. "round 3 · 75%" when 75% was
                    // actually round 1's score).
                    condition: null,
                };
                steps.push(current);
            }
        } else if (log.type === 'agent_complete') {
            const match = steps.findLast(s => s.kind === 'agent' && s.agent === log.agent && s.status === 'running');
            if (match) match.status = 'complete';
            current = null;
        } else if (log.type === 'tool_call' && current) {
            current.tools.push({ name: log.tool, status: 'running' });
        } else if (log.type === 'tool_result' && current) {
            const tool = current.tools.findLast(t => t.name === log.tool && t.status === 'running');
            if (tool) tool.status = 'complete';
        } else if (log.type === 'swarm_plan') {
            // Orchestrator emitted a plan. Push one placeholder subagent
            // pill per planned role so the timeline shows "Planning N
            // sub-agents" with role names BEFORE the first worker fires
            // a subagent_start. We synthesise a placeholder callId so the
            // matching real subagent_start can upgrade the pill to
            // status='running' in place (see subagentSelectors.js and the
            // alias-match branch below).
            //
            // Placeholder callId includes the log's ``nodeId`` when
            // present so two nodes that plan the same role name (e.g.
            // both plan a role called ``fetcher``) produce two DISTINCT
            // placeholders and never cross-upgrade the wrong pill.
            const roles = Array.isArray(log.roleIds) ? log.roleIds : [];
            for (const role of roles) {
                const nodeKey = log.nodeId || '';
                const placeholderId = `planned::${nodeKey}::${role}`;
                if (!steps.find(s => s.kind === 'subagent' && s.callId === placeholderId)) {
                    const placeholder = {
                        kind: 'subagent',
                        callId:      placeholderId,
                        nodeId:      log.nodeId || null,
                        alias:       role,
                        agentId:     '',
                        taskPreview: '',
                        status:      'planning',
                    };
                    _spliceSubagentStep(steps, placeholder);
                }
            }
        } else if (log.type === 'swarm_error') {
            // Swarm couldn't run — render the failure as a dedicated
            // pill so the user sees WHY (plan_validation_failed,
            // orchestrator_failure, manifest_failure) instead of just
            // waiting for the parent LLM's eventual paraphrase.
            _spliceSubagentStep(steps, {
                kind: 'subagent',
                callId:      `swarm_error::${log.code || 'unknown'}::${log.runId || ''}`,
                nodeId:      log.nodeId || null,
                alias:       log.code || 'swarm_error',
                agentId:     '',
                taskPreview: '',
                status:      'failed',
                error:       log.code || 'swarm_error',
                preview:     log.detail || '',
            });
        } else if (log.type === 'subagent_start') {
            // Sub-agent delegation pill. Rendered as a first-class step
            // so users can see who got called even when the parent had no
            // tool calls in this round. If a planning placeholder for the
            // same role already exists, upgrade it in place rather than
            // duplicating the pill.
            //
            // Placeholder is scoped by nodeId so we upgrade the pill
            // beneath the CORRECT parent agent — otherwise two workflow
            // nodes that both plan a ``fetcher`` role would upgrade one
            // arbitrary placeholder, leaving the other stuck at
            // status='planning' forever.
            const nodeKey = log.nodeId || '';
            const placeholderId = `planned::${nodeKey}::${log.alias}`;
            const placeholder = steps.findLast(
                s => s.kind === 'subagent' && s.callId === placeholderId
                    && s.status === 'planning',
            );
            if (placeholder) {
                placeholder.callId      = log.callId;
                placeholder.nodeId      = log.nodeId || placeholder.nodeId || null;
                placeholder.agentId     = log.agentId;
                placeholder.taskPreview = log.taskPreview || '';
                placeholder.tools       = Array.isArray(log.tools)  ? log.tools  : [];
                placeholder.skills      = Array.isArray(log.skills) ? log.skills : [];
                placeholder.status      = 'running';
                // Anchor the live elapsed timer at the moment the worker
                // actually starts running (not when it was merely planned).
                // ``log.ts`` is frozen at log creation (workflowStore.
                // addExecutionLog), so it survives every timeline rebuild —
                // using Date.now() here would reset running timers on each
                // rebuild.
                placeholder.startedAt   = log.ts || Date.now();
            } else {
                _spliceSubagentStep(steps, {
                    kind: 'subagent',
                    callId:      log.callId,
                    nodeId:      log.nodeId || null,
                    alias:       log.alias,
                    agentId:     log.agentId,
                    taskPreview: log.taskPreview,
                    tools:       Array.isArray(log.tools)  ? log.tools  : [],
                    skills:      Array.isArray(log.skills) ? log.skills : [],
                    status:      'running',
                    startedAt:   log.ts || Date.now(),
                });
            }
        } else if (log.type === 'subagent_complete') {
            const match = steps.findLast(
                s => s.kind === 'subagent' && s.callId === log.callId,
            );
            if (match) {
                match.status    = log.ok ? 'complete' : 'failed';
                match.durationS = log.durationS;
                match.error     = log.error || null;
                match.files     = log.files || [];
                match.preview   = log.preview || '';
            }
        } else if (log.type === 'loop_iter') {
            const existing = activeLoops.get(log.nodeId);
            const entry = {
                index: log.index ?? 0,
                total: log.total ?? null,
                mode:  log.mode || 'for_each',
                condition: existing ? existing.condition : null,
            };
            activeLoops.set(log.nodeId, entry);
            if (!loopStack.includes(log.nodeId)) loopStack.push(log.nodeId);
        } else if (log.type === 'loop_done') {
            activeLoops.delete(log.nodeId);
            const stackIdx = loopStack.lastIndexOf(log.nodeId);
            if (stackIdx >= 0) loopStack.splice(stackIdx, 1);
            steps.push({
                kind: 'loop_done',
                nodeId: log.nodeId,
                total: log.total ?? 0,
                maxHit: !!log.maxHit,
                status: 'complete',
            });
        } else if (log.type === 'loop_condition') {
            // MERGE with existing condition (don't replace wholesale). The
            // preceding `loop_iter_summary` log has already stamped the
            // score + changes text onto the condition — replacing the
            // whole object would wipe those out and leave the mid-run UI
            // showing "round N → continue" with no score / no changes.
            const entry = activeLoops.get(log.nodeId);
            const baseCondition = (entry && entry.condition) || {
                willContinue: false, caseResults: [], evalState: {},
            };
            const mergedEvalState = {
                ...(baseCondition.evalState || {}),
                ...(log.evalState || {}),
            };
            const snapshot = {
                ...baseCondition,
                willContinue: log.willContinue,
                caseResults: log.caseResults || [],
                evalState: mergedEvalState,
                // Flag from backend telling us the LLM judge is about to
                // score this iteration. The renderer uses it to show "LLM
                // evaluator (verifying…)" during the wait instead of the
                // misleading "self-reported by agent" pill.
                evaluatorPending: !!log.evaluatorPending,
            };
            if (entry) {
                entry.condition = snapshot;
            }
            // Stamp onto the most recent agent row for THIS loop, regardless
            // of whether it's still running. `loop_condition_eval` fires
            // AFTER `agent_complete` (the body finished, now we evaluate the
            // condition), so filtering by status='running' would miss every
            // round and leave the round chip without a score. Same lookup
            // shape as the `loop_iter_summary` branch below — single source
            // of truth for "which agent row owns this loop iteration".
            const targetAgent = steps.findLast(
                s => s.kind === 'agent' && s.loopNodeId === log.nodeId
            );
            if (targetAgent) targetAgent.condition = snapshot;
        } else if (log.type === 'loop_iter_summary') {
            // Synthesise an evalState so renderConditionSnapshot can show
            // the Confidence Score pill via the same path as while-mode's
            // real condition_eval — single source of truth.
            const entry = activeLoops.get(log.nodeId);
            const score = (typeof log.score === 'number') ? log.score : null;
            const baseCondition = (entry && entry.condition) || {
                willContinue: false, caseResults: [], evalState: {},
            };
            const evalState = { ...(baseCondition.evalState || {}) };
            if (score != null) evalState.score = score;
            if (log.changes) evalState.changes = log.changes;
            const merged = { ...baseCondition, evalState };
            if (entry) entry.condition = merged;
            const targetAgent = steps.findLast(
                s => s.kind === 'agent' && s.loopNodeId === log.nodeId
            );
            if (targetAgent) targetAgent.condition = merged;
        } else if (log.type === 'loop_iter_eval') {
            // Independent LLM-judge result. Overlays the (possibly already-
            // present) self-reported score with the judge's rubric-driven
            // score, attaches the full criterion breakdown + reasoning so
            // renderConditionSnapshot can render the expandable panel, and
            // overrides willContinue with the controller's decision (the
            // judge is more trustworthy than the raw case expression).
            const entry = activeLoops.get(log.nodeId);
            const evaluation = log.evaluation || null;
            const decision = log.decision || null;
            const baseCondition = (entry && entry.condition) || {
                willContinue: false, caseResults: [], evalState: {},
            };
            const evalState = { ...(baseCondition.evalState || {}) };
            if (evaluation && typeof evaluation.score === 'number') {
                evalState.score = evaluation.score;
            }
            const merged = {
                ...baseCondition,
                evalState,
                // willContinue from the controller (inverse of stop). When
                // the judge wasn't asked (no decision), keep the existing
                // baseCondition value — don't lie to the UI.
                willContinue: decision ? !decision.stop : baseCondition.willContinue,
                // New fields the renderer reads when present.
                evaluation,
                stopDecision: decision,
            };
            if (entry) entry.condition = merged;
            const targetAgent = steps.findLast(
                s => s.kind === 'agent' && s.loopNodeId === log.nodeId
            );
            if (targetAgent) targetAgent.condition = merged;
        }
    }

    if (steps.length === 0 && streamingAgent) {
        const name = parseStreamingAgent(streamingAgent);
        if (name) steps.push({ kind: 'agent', agent: name, tools: [], status: 'running' });
    }

    return steps;
}

function getThreadMeta(thread) {
    const count = thread.message_count || 0;
    const countLabel = count === 1 ? '1 message' : `${count} messages`;
    const time = formatRelativeTime(thread.last_updated);
    return [time, countLabel].filter(Boolean).join(' / ');
}

// Heuristic bounds for distinguishing a real multiple-choice menu from a
// numbered list that happens to appear inside structured agent output
// (e.g. a 10-slide outline). Tuned for realistic HITL UX: human-presented
// menus rarely exceed a handful of short labels.
const HITL_MAX_MENU_OPTIONS = 6;
const HITL_MAX_OPTION_LABEL_LEN = 120;

// Parse numbered options from an LLM prompt.
// Returns { question: string, options: string[] }
// options is empty if no numbered list was found.
function parseHitlOptions(prompt) {
    const lines = prompt.split('\n');
    const optionRegex = /^\s*(\d+)[.)]\s+(.+)$/;
    const questionLines = [];
    const options = [];
    let inOptions = false;

    for (const line of lines) {
        const match = line.match(optionRegex);
        if (match) {
            inOptions = true;
            options.push(match[2].trim());
        } else if (inOptions) {
            // A blank line is fine after options start; non-blank stops collection
            if (line.trim()) break;
        } else {
            questionLines.push(line);
        }
    }

    // Guardrail: a structured agent output like a slide outline
    //   "1. Title Slide …\n2. What is Data Science … (…10)"
    // would otherwise be mis-parsed as a 10-button choice menu, hiding the
    // body of each item. Real HITL option menus are short (2-6 choices) and
    // each option is a single short label, not a multi-sentence paragraph.
    // If the numbered list is long OR any item is long-form prose, treat the
    // whole prompt as structured content and render it as markdown instead.
    const looksLikeMenu =
        options.length >= 2 &&
        options.length <= HITL_MAX_MENU_OPTIONS &&
        options.every(opt => opt.length <= HITL_MAX_OPTION_LABEL_LEN);

    return {
        question: looksLikeMenu ? questionLines.join('\n').trim() : '',
        options: looksLikeMenu ? options : [],
    };
}

// --- HITL "before tool" helpers -------------------------------------------
// Goal: present a pending tool call as a friendly, human-readable card
// (summary sentence + collapsible pretty-printed args) instead of the raw
// JSON-stringified blob with visible \n / \" escapes.

function summarizeArgValue(val, maxLen = 60) {
    if (val == null) return '';
    // Slice before regex/stringify so huge values (e.g. 10k-line code blobs)
    // don't allocate a full whitespace-collapsed copy just to truncate.
    if (typeof val === 'string') {
        const slice = val.slice(0, maxLen * 4).replace(/\s+/g, ' ').trim();
        return slice.length > maxLen ? slice.slice(0, maxLen) + '...' : slice;
    }
    if (typeof val === 'number' || typeof val === 'boolean') return String(val);
    try {
        const s = JSON.stringify(val);
        return s.length > maxLen ? s.slice(0, maxLen) + '...' : s;
    } catch {
        return '';
    }
}

function buildToolSummary(toolName, args) {
    const name = (toolName || 'a tool').toString();
    const safe = (k) => (args && args[k] != null ? args[k] : '');
    switch (name) {
        case 'code_executor':
        case 'python':
        case 'python_executor':
            return 'wants to run a Python script';
        case 'web_search':
        case 'search':
        case 'google_search': {
            const q = summarizeArgValue(safe('query') || safe('q'), 80);
            return q
                ? `wants to search the web for "${q}"`
                : 'wants to search the web';
        }
        case 'read_file':
        case 'file_read': {
            const p = summarizeArgValue(safe('path') || safe('file'), 80);
            return p ? `wants to read the file ${p}` : 'wants to read a file';
        }
        case 'write_file':
        case 'file_write': {
            const p = summarizeArgValue(safe('path') || safe('file'), 80);
            return p ? `wants to write to the file ${p}` : 'wants to write a file';
        }
        case 'http_request':
        case 'fetch':
        case 'http': {
            const u = summarizeArgValue(safe('url'), 80);
            return u ? `wants to call the URL ${u}` : 'wants to make an HTTP request';
        }
        case 'shell':
        case 'bash':
        case 'run_command': {
            const c = summarizeArgValue(safe('command') || safe('cmd'), 80);
            return c ? `wants to run the command \`${c}\`` : 'wants to run a shell command';
        }
        case 'ask_human':
            return 'wants to ask you a question';
        default:
            return `wants to use the ${name} tool`;
    }
}

// Smart-parse free-form instructions typed into the before_tool HITL card
// textarea. Recognises:
//   • "don't use X" / "do not use X" / "remove X" / "skip X" / "without X"
//     → drop tool X from the pending list
//   • "use X" / "also use X" / "add X"  → add tool X (args={}) to the list
//   • "use only X"                       → drop everything else and keep/add X
//   • intent phrases (e.g. "also fetch a Jira issue") → add the best-scoring
//     catalog tool whose name+description overlaps the phrase's content words
//
// <X> precedence: existing pending call → catalog entry → unknown.
//
// Returns the next list, what changed, and any text the parser didn't consume
// so the textarea can retain free-form notes.

// Hoisted to module scope so the regex/Set objects aren't rebuilt on each
// call. The `/g` regexes are stateful; we reset `lastIndex` before each use.
const TOOL_EDIT_DROP_RE = /\b(?:don't\s+use|do\s+not\s+use|remove|skip|without)\s+([`'"]?[a-zA-Z0-9_./-]+[`'"]?)/gi;
const TOOL_EDIT_ADD_ONLY_RE = /\buse\s+only\s+([`'"]?[a-zA-Z0-9_./-]+[`'"]?)/gi;
const TOOL_EDIT_ADD_RE = /\b(?:also\s+use|use|add)\s+([`'"]?[a-zA-Z0-9_./-]+[`'"]?)/gi;
const TOOL_EDIT_CLAUSE_RE = /[^.;!?\n]+/g;
const TOOL_EDIT_SUBCLAUSE_RE = /[^,]+?(?:\s+and\s+|\s+also\s+|$)/g;
const TOOL_EDIT_INTENT_VERB_RE = /\b(fetch|get|list|find|search|lookup|read|create|make|build|send|post|update|edit|delete|remove|attach|need|want)\b/i;

// Words that carry no intent signal. Includes a few catalog-vocabulary nouns
// ('tool', 'tools', 'it', 'that', 'this') so the intent matcher can also use
// it as a "drop these literal unknowns once an intent add succeeded" set.
const TOOL_EDIT_STOPWORDS = new Set([
    'the', 'a', 'an', 'and', 'or', 'to', 'of', 'for', 'in', 'on', 'at',
    'with', 'about', 'related', 'also', 'please', 'can', 'we', 'i',
    'use', 'add', 'tool', 'tools', 'this', 'that', 'it', 'be', 'is',
    'are', 'will', 'should', 'would', 'could', 'do', 'does', 'did',
]);

function _tokenizeForIntent(s) {
    return (s || '')
        .toLowerCase()
        .split(/[^a-z0-9]+/)
        .filter((t) => t && t.length > 1 && !TOOL_EDIT_STOPWORDS.has(t));
}

function parseToolEdits(text, currentCalls, catalog) {
    const result = {
        nextCalls: [...(currentCalls || [])],
        addedNames: [],
        droppedNames: [],
        unknownNames: [],
        leftoverText: text,
    };
    if (!text || !text.trim()) return result;

    // Catalog accepts both legacy `string[]` and rich `{name, description}[]`
    // entries — the production /tools-catalog endpoint returns the latter.
    // We precompute lowercased haystack + tokens per entry so the intent loop
    // doesn't redo them per sub-clause.
    const catalogEntries = [];
    const catalogByLower = new Map();
    for (const entry of (catalog || [])) {
        if (!entry) continue;
        const name = typeof entry === 'string' ? entry : entry.name;
        if (!name) continue;
        const description = typeof entry === 'string' ? '' : (entry.description || '');
        const nameLower = name.toLowerCase();
        const hay = `${name} ${description}`.toLowerCase();
        catalogEntries.push({
            name,
            nameLower,
            hay,
            hayTokens: new Set(_tokenizeForIntent(hay)),
        });
        catalogByLower.set(nameLower, name);
    }

    const consumed = [];

    const pushAdd = (name, span) => {
        const exists = result.nextCalls.some(
            (c) => (c?.name || '').toLowerCase() === name.toLowerCase(),
        );
        if (!exists) {
            result.nextCalls.push({ id: makeId('human-add'), name, args: {} });
            result.addedNames.push(name);
        }
        if (span) consumed.push(span);
    };

    const resolveName = (raw) => {
        const q = raw.trim().replace(/^[`'"]+|[`'"]+$/g, '');
        if (!q) return null;
        const qLower = q.toLowerCase();
        const inList = result.nextCalls.find((c) => (c?.name || '').toLowerCase() === qLower);
        if (inList) return inList.name;
        const inCatalog = catalogByLower.get(qLower);
        if (inCatalog) return inCatalog;
        return { unknown: q };
    };

    // Score each catalog entry by content-word overlap with the fragment;
    // require ≥3 weighted points so a single common word doesn't trigger.
    const resolveIntent = (fragment) => {
        const tokens = _tokenizeForIntent(fragment);
        if (!tokens.length || !catalogEntries.length) return null;
        let best = null;
        let bestScore = 0;
        for (const entry of catalogEntries) {
            let score = 0;
            for (const t of tokens) {
                if (entry.hayTokens.has(t)) score += 2;
                else if (entry.hay.includes(t)) score += 1;
                // Boost: name hits are stronger than description hits.
                if (entry.nameLower.includes(t)) score += 1;
            }
            if (score > bestScore) {
                bestScore = score;
                best = entry.name;
            }
        }
        return bestScore >= 3 ? best : null;
    };

    let m;

    TOOL_EDIT_ADD_ONLY_RE.lastIndex = 0;
    while ((m = TOOL_EDIT_ADD_ONLY_RE.exec(text)) !== null) {
        const span = [m.index, m.index + m[0].length];
        const resolved = resolveName(m[1]);
        if (resolved && typeof resolved === 'object' && resolved.unknown) {
            result.unknownNames.push(resolved.unknown);
        } else if (resolved) {
            // "use only X" replaces the list rather than appending.
            result.nextCalls = [{ id: makeId('human-add'), name: resolved, args: {} }];
            result.addedNames.push(resolved);
        }
        consumed.push(span);
    }

    TOOL_EDIT_DROP_RE.lastIndex = 0;
    while ((m = TOOL_EDIT_DROP_RE.exec(text)) !== null) {
        const resolved = resolveName(m[1]);
        const targetName = (resolved && typeof resolved === 'string')
            ? resolved
            : (resolved && resolved.unknown) || m[1];
        const before = result.nextCalls.length;
        result.nextCalls = result.nextCalls.filter(
            (c) => (c?.name || '').toLowerCase() !== targetName.toLowerCase(),
        );
        if (result.nextCalls.length < before) result.droppedNames.push(targetName);
        consumed.push([m.index, m.index + m[0].length]);
    }

    TOOL_EDIT_ADD_RE.lastIndex = 0;
    while ((m = TOOL_EDIT_ADD_RE.exec(text)) !== null) {
        // Skip ranges already consumed by the "use only" pass.
        if (consumed.some(([s, e]) => m.index >= s && m.index < e)) continue;
        const span = [m.index, m.index + m[0].length];
        const resolved = resolveName(m[1]);
        if (resolved && typeof resolved === 'object' && resolved.unknown) {
            result.unknownNames.push(resolved.unknown);
            consumed.push(span);
            continue;
        }
        if (!resolved) continue;
        pushAdd(resolved, span);
    }

    // Intent pass — walks the unconsumed text clause-by-clause, requires an
    // intent verb so prose like "I don't like this" can't pull in a tool, and
    // splits on " and "/" also " so two intents in one sentence each match.
    if (catalogEntries.length) {
        const sortedConsumed = [...consumed].sort((a, b) => a[0] - b[0]);
        let masked = '';
        let cur = 0;
        for (const [s, e] of sortedConsumed) {
            if (s > cur) masked += text.slice(cur, s);
            masked += ' '.repeat(Math.max(0, e - s));
            cur = Math.max(cur, e);
        }
        if (cur < text.length) masked += text.slice(cur);

        TOOL_EDIT_CLAUSE_RE.lastIndex = 0;
        let cm;
        while ((cm = TOOL_EDIT_CLAUSE_RE.exec(masked)) !== null) {
            const clauseStart = cm.index;
            TOOL_EDIT_SUBCLAUSE_RE.lastIndex = 0;
            let sm;
            while ((sm = TOOL_EDIT_SUBCLAUSE_RE.exec(cm[0])) !== null) {
                const sub = sm[0];
                if (!TOOL_EDIT_INTENT_VERB_RE.test(sub)) continue;
                const intentName = resolveIntent(sub);
                if (!intentName) continue;
                const absStart = clauseStart + sm.index;
                pushAdd(intentName, [absStart, absStart + sub.length]);
            }
        }
    }

    // Stitch un-consumed slices back into leftoverText so free-form notes
    // (anything outside the matched fragments) stay in the textarea.
    if (consumed.length) {
        consumed.sort((a, b) => a[0] - b[0]);
        const parts = [];
        let cur = 0;
        for (const [s, e] of consumed) {
            if (s > cur) parts.push(text.slice(cur, s));
            cur = Math.max(cur, e);
        }
        if (cur < text.length) parts.push(text.slice(cur));
        result.leftoverText = parts.join(' ').replace(/\s+/g, ' ').trim();
    }

    // When an intent add succeeded, drop unknowns that are just generic
    // catalog vocabulary (e.g. literal "use tool" → "tool" reported unknown).
    // STOPWORDS already lists those words, so we reuse it as the filter.
    if (result.addedNames.length && result.unknownNames.length) {
        result.unknownNames = result.unknownNames.filter(
            (u) => !TOOL_EDIT_STOPWORDS.has(u.toLowerCase()),
        );
    }

    return result;
}

function ToolCallDetails({ toolName, argEntries }) {
    // Pre-format each arg once per render so totalLines, copyText, and the
    // JSX body don't each re-run safeString on the same values.
    const formatted = useMemo(
        () => argEntries.map(([k, v]) => [k, safeString(v)]),
        [argEntries],
    );

    // Lazy init: argEntries.reduce only runs on first render.
    const [open, setOpen] = useState(
        () => formatted.reduce((n, [, s]) => n + s.split('\n').length, 0) <= 6,
    );
    const [copied, setCopied] = useState(false);
    const copyTimerRef = useRef(null);

    // Clear the pending "Copied" reset on unmount so we don't setState on an
    // unmounted component when the user approves/denies within 1.5s.
    useEffect(() => () => {
        if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    }, []);

    const handleCopy = () => {
        const text = formatted.map(([k, s]) => `${k}:\n${s}`).join('\n\n');
        if (copyTextToClipboard(text)) {
            setCopied(true);
            if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
            copyTimerRef.current = setTimeout(() => setCopied(false), 1500);
        }
    };

    const hasArgs = formatted.length > 0;

    return (
        <div className="hitl-tool-details">
            <div className="hitl-tool-details-header">
                <span className="hitl-tool-details-name">
                    <span className="hitl-tool-details-name-label">Tool</span>
                    <code>{toolName}</code>
                </span>
                {hasArgs && (
                    <div className="hitl-tool-details-actions">
                        <button
                            type="button"
                            className="hitl-tool-copy-btn"
                            onClick={handleCopy}
                            title="Copy arguments"
                        >
                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                            </svg>
                            {copied ? 'Copied' : 'Copy'}
                        </button>
                        <button
                            type="button"
                            className="hitl-tool-toggle-btn"
                            onClick={() => setOpen((o) => !o)}
                            aria-expanded={open}
                        >
                            {open ? 'Hide details' : 'Show details'}
                            <svg
                                width="10"
                                height="10"
                                viewBox="0 0 24 24"
                                fill="none"
                                stroke="currentColor"
                                strokeWidth="2.5"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.15s' }}
                            >
                                <polyline points="6 9 12 15 18 9" />
                            </svg>
                        </button>
                    </div>
                )}
            </div>
            {hasArgs && open && (
                <div className="hitl-tool-details-body">
                    {formatted.map(([key, str]) => (
                        <div key={key} className="hitl-tool-arg">
                            <span className="hitl-tool-arg-label">{key}</span>
                            <pre className="hitl-tool-arg-code"><code>{str}</code></pre>
                        </div>
                    ))}
                </div>
            )}
            {!hasArgs && (
                <div className="hitl-tool-details-empty">No arguments</div>
            )}
        </div>
    );
}

function ChatPanel({ style, isActive = true }) {
    const [message, setMessage] = useState('');

    // Chat state lives in the workflow store so it survives mode switches
    // (preview ↔ edit) and stays intact while the user is browsing the
    // dashboard during a long-running execution. The setters wrap the
    // store's updater-aware actions so the rest of this file can keep using
    // `setMessages(prev => …)` / `setMessages([...])` indistinguishably.
    const messages = useWorkflowStore((s) => s.chatMessages);
    const setMessages = useWorkflowStore((s) => s.setChatMessages);
    const streamingContent = useWorkflowStore((s) => s.chatStreamingContent);
    const setStreamingContent = useWorkflowStore((s) => s.setChatStreamingContent);
    const streamingAgent = useWorkflowStore((s) => s.chatStreamingAgent);
    const setStreamingAgent = useWorkflowStore((s) => s.setChatStreamingAgent);
    // HITL (human-in-the-loop) state. The SSE handler sets this when the
    // backend sends a hitl_interrupt event; the JSX further below renders an
    // approval card per interruptType (before_tool / after_response /
    // ask_human) and POSTs the decision to the resume-stream endpoint.
    const hitlRequest = useWorkflowStore((s) => s.chatHitlRequest);
    const setHitlRequest = useWorkflowStore((s) => s.setChatHitlRequest);
    const hitlRedirectText = useWorkflowStore((s) => s.chatHitlRedirectText);
    const setHitlRedirectText = useWorkflowStore((s) => s.setChatHitlRedirectText);
    // Node-failure snapshot: mirrors the HITL pattern but is triggered by a
    // backend `node_failed` pending_interrupts row. The banner rendered from
    // this state lets the user re-run the failed node via /resume-stream.
    const failureSnapshot = useWorkflowStore((s) => s.chatFailureSnapshot);
    const setFailureSnapshot = useWorkflowStore((s) => s.setChatFailureSnapshot);
    // Auto-approve every subsequent before_tool pause for the open chat tab.
    // Per-turn list edits (×, smart instructions) are NEVER replayed for
    // future auto-approved pauses — this flag only suppresses the card
    // render. The override on the current resume request carries the
    // edited list; once that resume drains, the flag does nothing more.
    const sessionAutoApproveRef = useRef(false);
    // Per-card mutable copy of `hitlRequest.pendingToolCalls`. The user
    // shapes it via × buttons or smart instructions; Approve / Save & approve
    // forward THIS list to the engine as pending_tool_calls_override. Resets
    // whenever a new interrupt arrives (see effect below).
    const [editedToolCalls, setEditedToolCalls] = useState([]);
    // Inline message under the textarea when the smart parser couldn't
    // resolve a name. Cleared on every successful parse.
    const [toolEditError, setToolEditError] = useState('');
    // Transient status shown inside the running node's own timeline row while
    // a model retry / fallback is in progress. Shape: {agent, text} keyed by
    // the agent whose model is degrading, so in a multi-node run the line sits
    // under the node it belongs to (e.g. TEST_OCR) rather than the card header.
    // Ephemeral by design — cleared when that node completes / the run ends —
    // so routine self-healing doesn't clutter the transcript. The permanent
    // audit record lives in the Debug Log.
    const [fallbackStatus, setFallbackStatus] = useState(null);
    // {name -> descriptor} cache populated once per chat panel mount from
    // GET /tools-catalog. Drives the "add tool X" smart-parse resolution.
    const [toolCatalog, setToolCatalog] = useState([]);
    const threadId = useWorkflowStore((s) => s.chatThreadId);
    const setThreadId = useWorkflowStore((s) => s.setChatThreadId);
    // Mirror threadId into activeThreadId for ConfigPanel's Loop config
    // (connection-aware picker fetches /node-last-output by thread id).
    const setActiveThreadId = useWorkflowStore((s) => s.setActiveThreadId);
    useEffect(() => { setActiveThreadId(threadId); }, [threadId, setActiveThreadId]);

    // Shared helper: fetch /chat-pending with retry backoff and hydrate
    // the failureSnapshot when the server has written one. Used by
    // stopGeneration (after user Stop), the SSE error handler (after
    // a permanent node failure), and the self-heal effect below.
    // Retrying is essential because the backend's snapshot write happens
    // AFTER the ASGI stream closes — a single point-in-time poll misses it.
    const hydrateFailureSnapshotWithRetry = useCallback(async (tid, attempts = [0, 250, 600, 1200, 2500]) => {
        if (!tid) return false;
        for (const delay of attempts) {
            if (delay) await new Promise(r => setTimeout(r, delay));
            try {
                const pres = await fetch(
                    `${API_BASE}/chat-pending/${encodeURIComponent(tid)}`,
                    { headers: buildAuthHeaders() },
                );
                if (!pres.ok) continue;
                const pdata = await pres.json();
                const snap = pdata.pending;
                if (snap && (snap.reason === 'user_cancelled' || snap.reason === 'node_failed')) {
                    setFailureSnapshot({
                        threadId: snap.thread_id || tid,
                        nodeId: snap.node_id || '',
                        agent: snap.agent || 'Agent',
                        error: snap.error || (snap.reason === 'user_cancelled'
                            ? 'Run stopped by user.' : 'Node failed'),
                        errorType: snap.error_type || snap.reason || '',
                        completedNodes: snap.completed_nodes || [],
                        lastInput: snap.last_input || '',
                        documents: snap.documents || [],
                    });
                    return true;
                }
            } catch { /* non-fatal, next attempt */ }
        }
        return false;
    }, [setFailureSnapshot]);

    // Refresh the per-card pending-tool-calls draft whenever a NEW interrupt
    // arrives. This is the only place edits are cleared — the user's drops/
    // adds for the previous turn never bleed into the next pause.
    useEffect(() => {
        if (hitlRequest && hitlRequest.interruptType === 'before_tool') {
            setEditedToolCalls(Array.isArray(hitlRequest.pendingToolCalls) ? [...hitlRequest.pendingToolCalls] : []);
            setToolEditError('');
        } else {
            setEditedToolCalls([]);
            setToolEditError('');
        }
    }, [hitlRequest]);

    // One-shot tools-catalog fetch. Descriptions are kept alongside names so
    // the smart-parse intent matcher can resolve "fetch a Jira issue" →
    // jira_get_issue, not just exact-name lookups. Tolerant of catalog
    // failure (the parser falls back to "unknown" on miss).
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const res = await fetch(`${API_BASE}/tools-catalog`, { headers: buildAuthHeaders() });
                if (!res.ok) return;
                const data = await res.json();
                const rows = Array.isArray(data?.tools) ? data.tools : [];
                const entries = rows
                    .map((t) => (t && t.name)
                        ? { name: t.name, description: t.description || '' }
                        : null)
                    .filter(Boolean);
                if (!cancelled) setToolCatalog(entries);
            } catch {
                /* ignore — smart-parse just reports unknowns until catalog loads */
            }
        })();
        return () => { cancelled = true; };
    }, []);
    const [threads, setThreads] = useState([]);
    const [isLoadingHistory, setIsLoadingHistory] = useState(false);
    const [isHistoryOpen, setIsHistoryOpen] = useState(false);
    // Debug Log full-swap toggle. When true, the chat body (messages +
    // composer) is replaced by <DebugLogView/> inside the same .chat-main
    // section. Toggle is wired to a new .chat-icon-btn in the header.
    const [isDebugOpen, setIsDebugOpen] = useState(false);
    const [historySearch, setHistorySearch] = useState('');
    // null = unknown, 'ok' = healthy, 'error' = backend down, 'db_error' = backend up but DB issue
    const [backendStatus, setBackendStatus] = useState(null);
    const messagesEndRef = useRef(null);
    const historyButtonRef = useRef(null);
    const historyPanelRef = useRef(null);
    const textareaRef = useRef(null);
    const prevBackendStatus = useRef(null);
    const threadsLoadedRef = useRef(false);
    const abortRef = useRef(null);

    // Chat-pane attachments. Record shape:
    //   { id, file_name, file_type, file_size, parsed_text,
    //     blocked?, block_reason?, status: 'uploading'|'ready'|'error' }
    // On send, ready records are prepended as `[File: name]\n<parsed>` blocks
    // followed by `\n\nUser question: <text>`, matching Agent Builder's
    // attachment prompt shape. Cleared after each send.
    const [attachments, setAttachments] = useState([]);
    const [attachError, setAttachError] = useState('');
    const attachInputRef = useRef(null);

    // Chip → modal preview state. Stores the attachment id currently being
    // previewed, or null when no modal is open. The OCR pipeline runs with
    // backend defaults (engine auto-selected, language=en, no force_ocr) so
    // the user just drops a file and the answer streams back — no extra
    // toggles in the way.
    const [previewAttachmentId, setPreviewAttachmentId] = useState(null);

    // Message action-bar state: which message id currently shows the copied
    // checkmark, and a hidden anchor for the Teams deep-link share.
    const [copiedMsgId, setCopiedMsgId] = useState(null);
    const { teamsLinkRef, share: shareMessage, shareToTeams } = useShareActions();

    // 410 → file already consumed; banner shown via DownloadNotice. The
    // shared hook always routes through the auth'd helper and guards against
    // double downloads (see useGeneratedDownload).
    const {
        notice: downloadNotice,
        download: handleDownloadGenerated,
        isDownloading: isFileDownloading,
    } = useGeneratedDownload();
    // Mirror `attachments` to a ref so the upload handler can read .length
    // synchronously without re-running inside a setAttachments updater
    // (which React 18 StrictMode intentionally double-invokes in dev).
    const attachmentsRef = useRef(attachments);
    useEffect(() => { attachmentsRef.current = attachments; }, [attachments]);

    const isExecuting = useWorkflowStore((state) => state.isExecuting);
    const executionLogs = useWorkflowStore((state) => state.executionLogs);
    const executionResult = useWorkflowStore((state) => state.executionResult);
    const executionError = useWorkflowStore((state) => state.executionError);
    const setExecuting = useWorkflowStore((state) => state.setExecuting);

    // Self-healing failure banner — poll /chat-pending whenever a run
    // finishes (isExecuting goes true → false). This guarantees the
    // banner reappears even if:
    //   * the SSE `error` handler missed the retryable flag,
    //   * the initial 5-attempt hydration in stopGeneration timed out
    //     before the backend wrote the snapshot,
    //   * a resume itself failed and the SSE stream closed without
    //     a clean error event.
    // The retry-backed hydrator is a no-op when no snapshot exists,
    // so calling it here is safe on every completion. Placed here
    // (not earlier) so `isExecuting` is in scope — reading a `let`/
    // `const` before its declaration throws the "Cannot access
    // 'isExecuting' before initialization" TDZ error.
    const prevExecutingRef = useRef(false);
    useEffect(() => {
        const wasExecuting = prevExecutingRef.current;
        prevExecutingRef.current = isExecuting;
        if (wasExecuting && !isExecuting && threadId) {
            hydrateFailureSnapshotWithRetry(threadId);
        }
    }, [isExecuting, threadId, hydrateFailureSnapshotWithRetry]);
    const setExecutionResult = useWorkflowStore((state) => state.setExecutionResult);
    const setExecutionError = useWorkflowStore((state) => state.setExecutionError);
    const addExecutionLog = useWorkflowStore((state) => state.addExecutionLog);
    const clearExecutionLogs = useWorkflowStore((state) => state.clearExecutionLogs);
    const clearExecutionState = useWorkflowStore((state) => state.clearExecutionState);
    // Stop-preserving variant: resets live UI state but KEEPS the Debug Log
    // timeline (and marks the current run 'stopped') so an interrupted run
    // stays reviewable.
    const stopRunPreservingLog = useWorkflowStore((state) => state.stopRunPreservingLog);
    // Debug Log slice — `runContext` is read here so the view re-renders as
    // events arrive; the actions are called in the SSE handlers below.
    const runContext = useWorkflowStore((state) => state.runContext);
    const beginRunContext = useWorkflowStore((state) => state.beginRunContext);
    const appendRunEvent = useWorkflowStore((state) => state.appendRunEvent);
    const setRunContextFromComplete = useWorkflowStore((state) => state.setRunContextFromComplete);
    const setRunStatus = useWorkflowStore((state) => state.setRunStatus);
    const getWorkflowForExecution = useWorkflowStore((state) => state.getWorkflowForExecution);
    const isWorkflowValid = useWorkflowStore((state) => state.isWorkflowValid);
    const nodes = useWorkflowStore((state) => state.nodes);
    const setNodeActive = useWorkflowStore((state) => state.setNodeActive);
    const clearNodeActive = useWorkflowStore((state) => state.clearNodeActive);
    const clearAllActiveNodes = useWorkflowStore((state) => state.clearAllActiveNodes);
    const setLoopProgress = useWorkflowStore((state) => state.setLoopProgress);
    const clearLoopProgress = useWorkflowStore((state) => state.clearLoopProgress);
    const workflowId = useWorkflowStore((state) => state.workflowId);
    const workflowName = useWorkflowStore((state) => state.workflowName);

    // Remember the active thread per workflow so a reload reopens the same
    // conversation (loadThreads consumes this as its preferredThreadId).
    useEffect(() => {
        if (workflowId && threadId) saveActiveThread('workflow', workflowId, threadId);
    }, [workflowId, threadId]);

    // Restore any unsent composer text for the active (workflow, thread) after
    // a reload, then keep it persisted as the user types so it survives too.
    // Keyed per-thread so switching threads shows that thread's own draft.
    const draftSeededKeyRef = useRef(null);
    useEffect(() => {
        if (!workflowId || !threadId) return;
        const key = `${workflowId}::${threadId}`;
        if (draftSeededKeyRef.current === key) return;
        draftSeededKeyRef.current = key;
        setMessage(loadComposerDraft('workflow', workflowId, threadId));
    }, [workflowId, threadId]);
    useEffect(() => {
        if (!workflowId || !threadId) return;
        saveComposerDraft('workflow', workflowId, threadId, message);
    }, [message, workflowId, threadId]);
    const setViewingChat = useWorkflowStore((state) => state.setViewingChat);
    // Chat-panel run-settings flag — forwarded to the engine in the
    // /run-stream payload so all otherwise-unpinned nodes obey the
    // user's run-level choice. Per-node pins
    // (`disable_subagents=true` / `enable_subagents=true`) still win
    // because the engine reads the per-node bits first.
    const runSubagentsEnabled = useWorkflowStore((state) => state.runSubagentsEnabled);
    // Used by "Save & approve" to persist tool-list edits onto the agent
    // node and PUT the workflow so the change survives reload.
    const updateNodeData = useWorkflowStore((state) => state.updateNodeData);
    const updateWorkflowRecord = useDashboardStore((state) => state.updateWorkflow);

    const findNodeForExecutionEvent = useCallback((payload) => {
        const nodeId = payload?.node_id;
        if (nodeId) return nodes.find(n => n.id === nodeId);
        const agent = payload?.agent;
        return nodes.find(n => n.data?.name === agent);
    }, [nodes]);

    // Debug Log support: a nodeId → human label map and a helper that
    // appends a normalised row to runContext. Memoised on `nodes` so the
    // SSE event loop doesn't walk the array per event.
    //
    // The mapping uses node.data.name (matches AgentNode's <h4>{data.name}>)
    // and falls back to the node id so an unknown id still renders as a row.
    const nodeLabelById = useMemo(() => {
        const out = {};
        for (const n of nodes) {
            out[n.id] = n?.data?.name || n.id;
        }
        return out;
    }, [nodes]);

    // Per-node metadata bag used by pushDebugRow to enrich a row with
    // type-specific badges and KB / subflow hints. The engine itself does
    // not emit these as SSE events (KB is server-side log only, subflow
    // ref is just a workflow_id), so we have to lift them from the
    // workflow definition at row-build time.
    const nodeMetaById = useMemo(() => {
        const out = {};
        for (const n of nodes) {
            const d = n?.data || {};
            const kbMode = d?.knowledge?.mode;
            const kbActive = kbMode && kbMode !== 'none';
            out[n.id] = {
                type: n.type || 'agent',
                kbActive: !!kbActive,
                kbMode: kbActive ? kbMode : null,
                subflowRefName: d?.subflowRefName || d?.ref_name || null,
                loopMode: d?.loopMode || d?.mode || null,
            };
        }
        return out;
    }, [nodes]);

    // Maps the internal node.type to a user-facing badge label.
    // Used by DebugLogView via the row's `kind` field so each row
    // shows its node-type even without expanding the JSON.
    const nodeKindLabel = useCallback((type) => {
        switch (type) {
            case 'start':     return 'Start';
            case 'end':       return 'End';
            case 'condition': return 'Condition';
            case 'loop':      return 'Loop';
            case 'subflow':   return 'Subflow';
            case 'agent':
            default:          return 'Agent';
        }
    }, []);

    // Stable, side-effect-free row pusher. Resolves the label from the
    // payload's node_id (or `agent` fallback), looks up node-type metadata
    // and a KB hint from the workflow definition, then forwards to the
    // store. Callers can override anything by setting it on the `row`.
    const pushDebugRow = useCallback((row) => {
        if (!row) return;
        const nodeId = row.nodeId || row.raw?.node_id || null;
        const fallbackLabel = row.raw?.agent || row.raw?.node_id || '';
        const meta = nodeId ? nodeMetaById[nodeId] : null;
        const resolvedLabel = row.nodeLabel || nodeLabelById[nodeId] || fallbackLabel;
        const enriched = {
            ...row,
            nodeId,
            nodeLabel: resolvedLabel,
        };
        // Stamp a node-kind chip onto the row whenever we can resolve one.
        // `row.kind` always wins so non-node events (e.g. "Run started",
        // "Sub-agent {alias}", "Swarm planned") can pass their own.
        if (!enriched.kind) {
            // The engine prefixes subflow agents like "Outer \u25b8 Inner".
            // Detect that and badge the row as Subflow rather than Agent
            // so the user sees nested execution distinctly.
            if (typeof resolvedLabel === 'string' && resolvedLabel.includes('\u25b8')) {
                enriched.kind = 'Subflow';
            } else {
                enriched.kind = meta ? nodeKindLabel(meta.type) : null;
            }
        }
        // KB hint: surface "Knowledge: <mode>" as a sub-line so the user
        // knows the agent's response was grounded in KB context, even
        // though the engine doesn't emit a discrete KB-injection event.
        if (meta?.kbActive && !enriched.kbHint) {
            enriched.kbHint = `Knowledge base: ${meta.kbMode}`;
        }
        appendRunEvent(enriched);
    }, [appendRunEvent, nodeLabelById, nodeMetaById, nodeKindLabel]);

    // Derived once per execution log update — the thinking card consumes this
    // every render while isExecuting is true, so memoise to avoid an O(n) walk
    // on every keystroke.
    const agentTimeline = useMemo(
        () => buildAgentTimeline(executionLogs, streamingAgent),
        [executionLogs, streamingAgent],
    );

    // Live "N sub-agents working" count derived from the run log.
    // Implemented in subagentSelectors.js so it's unit-testable
    // without mounting this whole component.
    const activeSubagents = useMemo(
        () => selectActiveSubagents(executionLogs),
        [executionLogs],
    );
    // Full list (running + complete + failed) powers the accordion the
    // user can drill into. Selector preserves start-order so the rows
    // stay stable across renders even as completion events arrive.
    const allSubagents = useMemo(
        () => selectAllSubagents(executionLogs),
        [executionLogs],
    );

    // Trigger-execution stream: when a scheduled run completes for THIS
    // workflow we inject it as a chat bubble so the user sees the output
    // inline. The bell skips a toast in this case (handled in
    // TriggerNotifications via `isViewingChat`).
    const recentExecutions = useTriggersStore((state) => state.recentExecutions);
    const loadNotifications = useTriggersStore((state) => state.loadNotifications);
    const markSeen = useTriggersStore((state) => state.markSeen);
    const seenExecutionsRef = useRef(new Set());

    // Tell the bell "user is on the canvas for this workflow" while the
    // chat is the active surface. ChatPanel is now kept mounted across
    // mode toggles (edit ↔ preview) so we gate this on `isActive` instead
    // of mount/unmount — otherwise the bell would suppress trigger toasts
    // even while the user is staring at the canvas node config.
    useEffect(() => {
        if (!isActive) return undefined;
        setViewingChat(true);
        const tick = () => {
            if (typeof document === 'undefined' || document.visibilityState === 'visible') {
                loadNotifications();
            }
        };
        const id = setInterval(tick, 5000);
        tick();
        return () => {
            setViewingChat(false);
            clearInterval(id);
        };
    }, [isActive, setViewingChat, loadNotifications]);

    // Inject newly-completed trigger runs for this workflow as chat messages.
    useEffect(() => {
        if (!Array.isArray(recentExecutions) || !workflowId) return;
        const newOnes = [];
        recentExecutions.forEach((exec) => {
            if (exec.target_kind !== 'workflow') return;
            if (exec.target_id !== workflowId) return;
            if (exec.status === 'running') return;
            if (seenExecutionsRef.current.has(exec.id)) return;
            // Cap the Set so a long-lived editor session doesn't accumulate
            // one entry per executed run forever. 500 is well above any
            // reasonable open-tab session count.
            if (seenExecutionsRef.current.size >= 500) {
                const first = seenExecutionsRef.current.values().next().value;
                if (first !== undefined) seenExecutionsRef.current.delete(first);
            }
            seenExecutionsRef.current.add(exec.id);
            newOnes.push(exec);
        });
        if (newOnes.length === 0) return;

        setMessages((prev) => {
            const next = [...prev];
            newOnes.forEach((exec) => {
                const when = exec.started_at
                    ? new Date(exec.started_at).toLocaleString('en-IN', {
                          timeZone: 'Asia/Kolkata',
                          month: 'short', day: '2-digit',
                          hour: '2-digit', minute: '2-digit', hour12: true,
                      }) + ' IST'
                    : '';
                // Use the existing 'user' / 'assistant' types so the
                // standard renderer styles them as a chat exchange. The
                // timestamp + "Scheduled run" badge lives inline in the
                // markdown so it shows up without touching the renderer.
                next.push({
                    type: 'user',
                    content: `⏰ Scheduled run · ${when}\n\n${exec.input_text || '(triggered)'}`,
                    fromTrigger: true,
                });
                if (exec.status === 'error') {
                    next.push({
                        type: 'error',
                        content: `Scheduled run failed at ${when}: ${exec.error || 'unknown error'}`,
                    });
                } else {
                    next.push({
                        type: 'assistant',
                        content: `_Completed ${when}_\n\n${exec.output || '(no output)'}`,
                        fromTrigger: true,
                    });
                }
            });
            return next;
        });

        // Once we've shown them in chat, mark them seen so the bell badge
        // doesn't double-count and so the toast won't pop after the user
        // navigates away.
        newOnes.forEach((exec) => { if (!exec.seen) markSeen(exec.id); });
    }, [recentExecutions, workflowId, markSeen]);

    const loadChatHistory = async (targetThreadId) => {
        if (!targetThreadId) {
            setMessages([]);
            return;
        }

        setIsLoadingHistory(true);
        setStreamingContent('');
        setStreamingAgent('');
        setFallbackStatus(null);
        // Clear any stale HITL card from the previous thread before
        // hydrating; setHitlRequest(null) prevents one thread's pause card
        // from briefly bleeding into another while the fetch runs.
        setHitlRequest(null);
        setHitlRedirectText('');
        try {
            // API_BASE is a compile-time constant (never user-supplied).
            // targetThreadId is validated INLINE, in this scope, against a
            // positive allow-list (SAFE_PATH_SEGMENT_RE) immediately before
            // use — a value that does not match in full is REJECTED, not
            // stripped-and-continued. No path-traversal or host-injection is
            // possible. No SSRF vector exists.
            const rawThreadId = String(targetThreadId);
            if (!SAFE_PATH_SEGMENT_RE.test(rawThreadId)) throw new Error('Invalid thread ID');
            const safeThreadId = rawThreadId;
            const res = await fetch(`${API_BASE}/chat-history/${encodeURIComponent(safeThreadId)}`, { headers: buildAuthHeaders() });
            if (!res.ok) {
                throw new Error(`Failed to load history (${res.status})`);
            }
            const data = await res.json();
            setMessages(mapHistoryToUiMessages(data.messages));

            // HITL hydration: if this thread is paused server-side, fetch
            // the snapshot and re-render the HITL card. Best-effort —
            // failing to fetch the pending state should not break history
            // loading.
            try {
                const pres = await fetch(
                    `${API_BASE}/chat-pending/${encodeURIComponent(safeThreadId)}`,
                    { headers: buildAuthHeaders() },
                );
                if (pres.ok) {
                    const pdata = await pres.json();
                    const snap = pdata.pending;
                    if (snap) {
                        const reason = snap.reason || 'after_response';
                        if (reason === 'node_failed') {
                            // Failure snapshot — rendered by a dedicated
                            // banner, not the HITL card. Do NOT populate
                            // setHitlRequest here or the HITL flow would
                            // hijack the resume click.
                            setFailureSnapshot({
                                threadId: snap.thread_id || targetThreadId,
                                nodeId: snap.node_id || '',
                                agent: snap.agent || 'Agent',
                                error: snap.error || 'Node failed',
                                errorType: snap.error_type || '',
                                completedNodes: snap.completed_nodes || [],
                                lastInput: snap.last_input || '',
                                documents: snap.documents || [],
                            });
                        } else {
                            let interruptType = reason;
                            let question = '';
                            let options = [];
                            let prompt = '';
                            if (reason === 'ask_human') {
                                question = (snap.ask_human && snap.ask_human.question) || '';
                                options = ((snap.ask_human && snap.ask_human.options) || []);
                                prompt = question;
                            } else if (reason === 'before_tool') {
                                // prompt is a fallback summary; the card renders
                                // from pendingToolCalls directly.
                                const calls = snap.pending_tool_calls || [];
                                const first = calls[0] || {};
                                prompt = buildToolSummary(first.name, first.args || {});
                            } else {
                                prompt = snap.output || '';
                            }
                            setHitlRequest({
                                threadId: snap.thread_id || targetThreadId,
                                interruptType,
                                agent: snap.agent || 'Agent',
                                nodeId: snap.node_id || '',
                                prompt, question, options,
                                pendingToolCalls: snap.pending_tool_calls || [],
                            });
                            // Clear any stale failure banner left over from a
                            // previous run of this thread.
                            setFailureSnapshot(null);
                        }
                    }
                }
            } catch { /* non-fatal */ }
        } catch (err) {
            setMessages((prev) => [...prev, { type: 'error', content: `History load failed: ${err.message}` }]);
        } finally {
            setIsLoadingHistory(false);
        }
    };

    const loadThreads = async (workflowIdValue, preferredThreadId = null) => {
        if (!workflowIdValue) return;

        try {
            // API_BASE is a compile-time constant (never user-supplied).
            // workflowIdValue is validated INLINE, in this scope, against a
            // positive allow-list (SAFE_PATH_SEGMENT_RE) immediately before
            // use — a value that does not match in full is REJECTED, not
            // stripped-and-continued. No path-traversal or host-injection is
            // possible. No SSRF vector exists.
            const rawWorkflowId = String(workflowIdValue);
            if (!SAFE_PATH_SEGMENT_RE.test(rawWorkflowId)) throw new Error('Invalid workflow ID');
            const safeWorkflowId = rawWorkflowId;
            const res = await fetch(`${API_BASE}/chat-threads/${encodeURIComponent(safeWorkflowId)}`, { headers: buildAuthHeaders() });
            if (!res.ok) {
                throw new Error(`Failed to load threads (${res.status})`);
            }
            const data = await res.json();
            const fetchedThreads = data.threads || [];
            setThreads(fetchedThreads);
            threadsLoadedRef.current = true;

            // Only honor a preferred thread that still exists in the DB — a
            // stale id (deleted thread, or one that never persisted because it
            // had no messages) falls through to the latest/new logic below.
            if (preferredThreadId && fetchedThreads.some((t) => t.thread_id === preferredThreadId)) {
                const safePreferred = String(preferredThreadId).replace(/[^a-zA-Z0-9_\-]/g, '');
                setThreadId(safePreferred);
                await loadChatHistory(safePreferred);
                return;
            }

            if (fetchedThreads.length > 0) {
                const latestThreadId = String(fetchedThreads[0].thread_id).replace(/[^a-zA-Z0-9_\-]/g, '');
                setThreadId(latestThreadId);
                await loadChatHistory(latestThreadId);
            } else {
                const newThreadId = createThreadId(workflowIdValue);
                setThreadId(newThreadId);
                setMessages([]);
            }
        } catch (err) {
            threadsLoadedRef.current = false;
            const fallbackThreadId = createThreadId(workflowIdValue);
            setThreadId(fallbackThreadId);
            setThreads([]);
            // Don't show error message — backend may still be starting up
        }
    };

    // Refresh only the threads sidebar list without reloading current chat history.
    // Used after execution so in-memory messages are preserved.
    const refreshThreadsList = async (workflowIdValue) => {
        if (!workflowIdValue) return;
        try {
            const res = await fetch(`${API_BASE}/chat-threads/${encodeURIComponent(workflowIdValue)}`, { headers: buildAuthHeaders() });
            if (!res.ok) return;
            const data = await res.json();
            setThreads(data.threads || []);
            threadsLoadedRef.current = true;
        } catch {
            // Silently ignore — threads list is cosmetic after execution
        }
    };

    // `streamingContent` updates on every SSE token (~50-100 ms). Calling
    // scrollIntoView with `behavior: 'smooth'` on every token starts a new
    // ~300 ms animation before the previous one finishes, so the chat pane
    // visibly judders/flickers while the LLM is generating. We use an
    // instant scroll during streaming (each token just pins the viewport
    // to the bottom with no animation) and reserve the smooth scroll for
    // the discrete moments when a full message lands or the thinking
    // timeline updates.
    const isStreamingRef = useRef(false);
    isStreamingRef.current = Boolean(streamingContent);

    const scrollToBottom = (smooth) => {
        messagesEndRef.current?.scrollIntoView({
            behavior: smooth ? 'smooth' : 'auto',
        });
    };

    useEffect(() => {
        scrollToBottom(!isStreamingRef.current);
    }, [messages, executionLogs, streamingContent]);

    // When the workflow identity changes (user opened a different workflow),
    // wipe the in-store chat slice so messages from workflow A don't briefly
    // flash inside workflow B's preview pane before history reloads. When
    // ChatPanel just remounts for the same workflow (mode toggle), the
    // store's `chatOwnerWorkflowId` already matches and this is a no-op —
    // which is exactly what preserves the chat across preview ↔ edit swaps.
    const resetChatStateForWorkflow = useWorkflowStore((s) => s.resetChatStateForWorkflow);
    const chatOwnerWorkflowId = useWorkflowStore((s) => s.chatOwnerWorkflowId);
    useEffect(() => {
        threadsLoadedRef.current = false;
        // Only reload threads when the workflow actually changes. Re-running
        // loadThreads on every remount would overwrite in-flight streamed
        // messages with the DB snapshot.
        if (chatOwnerWorkflowId !== workflowId) {
            resetChatStateForWorkflow(workflowId);
            // Reopen the thread the user last had active for this workflow so a
            // reload lands on the same conversation, not just the latest one.
            loadThreads(workflowId, loadActiveThread('workflow', workflowId));
            setIsHistoryOpen(false);
        }
    }, [workflowId, chatOwnerWorkflowId, resetChatStateForWorkflow]);

    // Poll backend health every 30s — skip when the tab is hidden so a
    // backgrounded editor doesn't ping /health forever.
    useEffect(() => {
        const checkHealth = async () => {
            if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
            try {
                const res = await fetch(`${API_BASE}/health`, { headers: buildAuthHeaders(), signal: AbortSignal.timeout(3000) });
                if (!res.ok) { setBackendStatus('error'); return; }
                const data = await res.json();
                setBackendStatus(data.db === 'ok' ? 'ok' : 'db_error');
            } catch {
                setBackendStatus('error');
            }
        };
        checkHealth();
        const interval = setInterval(checkHealth, 30000);
        return () => clearInterval(interval);
    }, []);

    // Auto-retry loading threads when backend comes online after being down
    useEffect(() => {
        const wasDown = prevBackendStatus.current !== 'ok';
        const nowUp = backendStatus === 'ok';
        if (nowUp && wasDown && workflowId && !threadsLoadedRef.current) {
            loadThreads(workflowId);
        }
        prevBackendStatus.current = backendStatus;
    }, [backendStatus, workflowId]);

    useEffect(() => {
        const onDocumentClick = (event) => {
            if (!isHistoryOpen) return;
            const clickedHistoryButton = historyButtonRef.current?.contains(event.target);
            const clickedHistoryPanel = historyPanelRef.current?.contains(event.target);
            if (!clickedHistoryButton && !clickedHistoryPanel) {
                setIsHistoryOpen(false);
            }
        };

        document.addEventListener('mousedown', onDocumentClick);
        return () => document.removeEventListener('mousedown', onDocumentClick);
    }, [isHistoryOpen]);

    const handleNewChat = () => {
        const newThreadId = createThreadId(workflowId);
        setThreadId(newThreadId);
        setIsHistoryOpen(false);
        setHistorySearch('');
        setStreamingContent('');
        setStreamingAgent('');
        setFallbackStatus(null);
        // HITL state lives in the global workflow store (so it survives mode
        // switches), but it is a single slot per workflow — not keyed by
        // thread. Without explicit clearing here, a pending approval from
        // the previous thread would keep rendering on the brand-new chat.
        // handleThreadSelect already clears via loadChatHistory(); New Chat
        // must do the same manually because it never hits that codepath.
        setHitlRequest(null);
        setHitlRedirectText('');
        // "Allow all this session" is scoped to the conversation the user
        // granted it in. Starting a new chat is a new conversation, so revoke
        // it — otherwise a blanket tool approval silently follows the user
        // into unrelated threads for as long as the tab stays open.
        sessionAutoApproveRef.current = false;
        setThreads((prev) => ([
            {
                thread_id: newThreadId,
                title: 'New chat',
                message_count: 0,
                last_updated: new Date().toISOString(),
                last_message_preview: '',
            },
            ...prev.filter((t) => t.thread_id !== newThreadId),
        ]));
        setMessages([]);
        clearAllActiveNodes();
        clearExecutionState();
    };

    const handleThreadSelect = async (selectedThreadId) => {
        if (!selectedThreadId || selectedThreadId === threadId) return;
        setThreadId(selectedThreadId);
        setIsHistoryOpen(false);
        // Switching conversations revokes a session-wide tool approval for the
        // same reason New Chat does — the grant belongs to the thread it was
        // given in, not to the browser tab.
        sessionAutoApproveRef.current = false;
        await loadChatHistory(selectedThreadId);
    };

    const handleDeleteThread = async (e, deletedThreadId) => {
        e.stopPropagation();
        try {
            await fetch(`${API_BASE}/chat-threads/${encodeURIComponent(deletedThreadId)}`, {
                method: 'DELETE',
                headers: buildAuthHeaders(),
            });
            setThreads(prev => prev.filter(t => t.thread_id !== deletedThreadId));
            if (deletedThreadId === threadId) {
                handleNewChat();
            }
        } catch {
            // silently ignore
        }
    };

    const filteredThreads = threads.filter((thread) => {
        const q = historySearch.trim().toLowerCase();
        if (!q) return true;
        const title = threadTitle(thread).toLowerCase();
        const preview = (thread.last_message_preview || '').toLowerCase();
        return title.includes(q) || preview.includes(q);
    });
    const groupedThreads = groupThreads(filteredThreads);
    const recentThreads = threads.filter((thread) => thread.thread_id !== threadId).slice(0, 3);
    const activeThread = threads.find((thread) => thread.thread_id === threadId);
    const lastUserMessage = [...messages].reverse().find((msg) => msg.type === 'user');

    // Restore the composer text AND attachment chips from the last user
    // message. The document parsed_text is already persisted in the backend
    // snapshot state and will be re-injected into the resumed run
    // automatically — we only need to restore the UI chips so the user
    // sees what was attached and the resume-stream payload carries the
    // correct attachment metadata.
    const handleRestorePrompt = useCallback(() => {
        if (!lastUserMessage) return;
        setMessage(safeString(lastUserMessage.content));

        // Prefer documents from the failure snapshot (richer metadata,
        // sourced from the persisted backend state). Fall back to the
        // message-level attachments array (file_name / file_type / file_size
        // only) when the snapshot predates the documents field.
        const snapDocs = Array.isArray(failureSnapshot?.documents)
            ? failureSnapshot.documents
            : [];
        const msgAttachments = Array.isArray(lastUserMessage.attachments)
            ? lastUserMessage.attachments
            : [];
        const source = snapDocs.length > 0 ? snapDocs : msgAttachments;

        if (source.length > 0) {
            // Rebuild attachment records in the 'ready' state. parsed_text
            // is intentionally left empty here — the backend already has the
            // full content in the persisted snapshot state and will re-inject
            // it into the resumed run. The chips are purely for UI feedback.
            const restored = source.map((d, i) => ({
                id: `restored-${Date.now()}-${i}`,
                file_name: d.file_name || d.filename || 'document',
                file_type: d.file_type || '',
                file_size: d.file_size || 0,
                char_count: d.char_count || 0,
                page_count: d.page_count || 0,
                parsed_text: '',   // content lives in backend snapshot state
                status: 'ready',
                kind: 'document',
                progress: 100,
                blocked: false,
                block_reason: '',
                engine: '',
                warnings: [],
                _restored: true,   // flag so send path knows not to re-upload
            }));
            setAttachments(restored);
            setAttachError('');
        }

        // Scroll the textarea into view so the user sees the restored text.
        setTimeout(() => textareaRef.current?.focus(), 0);
    }, [lastUserMessage, failureSnapshot]);

    const autoResizeTextarea = useCallback(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = Math.min(el.scrollHeight, 200) + 'px';
    }, []);

    useEffect(() => {
        autoResizeTextarea();
    }, [message, autoResizeTextarea]);

    // Attachments are routed through Build Studio's `/agent-runner/attachment`
    // endpoint so Workflow Builder and Agent Builder use the same parser,
    // 25 MB limit, and supported document formats.
    const handleAttachClick = useCallback(() => {
        if (isExecuting) return;
        setAttachError('');
        attachInputRef.current?.click();
    }, [isExecuting]);

    const removeAttachment = useCallback((id) => {
        setAttachments(prev => prev.filter(a => a.id !== id));
        // Clear the sticky upload error once the offending attachment is gone.
        setAttachError('');
    }, []);

    // Single-file upload helper. Documents flow through /agent-runner/attachment
    // (OCR/text extraction). Images flow through /agent-runner/image-asset so
    // they are saved as sandbox files the agent can reference by path.
    const uploadAttachmentFile = useCallback(async (file, { forceOcr = false, describeVisuals = true } = {}) => {
        const fd = new FormData();
        fd.append('file', file);
        const isImage = isImageAsset(file.name);
        const endpoint = isImage ? '/agent-runner/image-asset' : '/agent-runner/attachment';
        if (!isImage && forceOcr) fd.append('force_ocr', 'true');
        if (isImage && describeVisuals) fd.append('describe_visuals', 'true');
        const res = await fetch(`${API_BASE}${endpoint}`, {
            method: 'POST',
            credentials: 'include',
            headers: buildAuthHeaders({ omitContentType: true }),
            body: fd,
        });
        let data = null;
        try { data = await res.json(); } catch { /* non-JSON error body */ }
        if (!res.ok) {
            const reason = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
            const err = new Error(reason);
            err.detail = reason;
            throw err;
        }
        return data || {};
    }, []);

    // Maps the backend's `/agent-runner/attachment` or `/agent-runner/image-asset`
    // response envelope onto the chat-pane attachment record shape. Field names
    // diverge from the shared AttachmentChip's camelCase props (we adapt at
    // render time) so existing send/format helpers continue to read
    // `parsed_text`/`file_name` unchanged. The OCR metadata (engine, warnings,
    // etc.) is additive.
    const _readyRecordFromResponse = useCallback((data, file, phId) => {
        const ext = (file.name.split('.').pop() || '').toLowerCase();
        const isImage = isImageAsset(file.name);
        return {
            id: phId,
            file_name: data.filename || file.name,
            file_type: ext,
            file_size: file.size,
            parsed_text: data.text || '',
            blocked: false,
            block_reason: '',
            progress: 100,
            status: 'ready',
            kind: isImage ? 'image' : 'document',
            // For image assets: the absolute path and sandbox filename the
            // agent should use. The absolute path works because the code
            // executor runs on the same host as the backend.
            asset_path: isImage ? (data.asset_path || '') : undefined,
            asset_name: isImage ? (data.sandbox_name || data.disk_name || data.filename || file.name) : undefined,
            download_url: isImage ? (data.download_url || '') : undefined,
            engine: data.engine || '',
            warnings: Array.isArray(data.warnings) ? data.warnings : [],
            images_extracted: data.images_extracted || 0,
            tables_extracted: data.tables_extracted || 0,
            cache_hit: !!data.cache_hit,
            char_count: data.char_count || 0,
            original_char_count: data.original_char_count || 0,
            truncated: !!data.truncated,
            page_count: data.page_count || 0,
        };
    }, []);

    const handleFilesPicked = useCallback(async (e) => {
        const fileList = Array.from(e.target.files || []);
        e.target.value = '';
        // Cancelled file picker — clear any stale error so the previous
        // failed upload message doesn't linger on screen.
        if (fileList.length === 0) {
            setAttachError('');
            return;
        }

        const currentCount = attachmentsRef.current.length;
        const room = CHAT_ATTACH_MAX_FILES - currentCount;
        if (room <= 0) {
            setAttachError(`At most ${CHAT_ATTACH_MAX_FILES} files per message`);
            return;
        }
        const picked = fileList.slice(0, room);
        if (fileList.length > room) {
            setAttachError(`At most ${CHAT_ATTACH_MAX_FILES} files per message — extra files were skipped`);
        }
        if (picked.length === 0) return;

        const placeholders = picked.map((f) => ({
            id: _newAttachId(),
            file_name: f.name,
            file_type: (f.name.split('.').pop() || '').toLowerCase(),
            file_size: f.size,
            parsed_text: '',
            blocked: false,
            block_reason: '',
            progress: 0,
            status: 'uploading',
            // Stash the File object so "Retry with OCR" can re-upload the
            // exact bytes the user picked without prompting them again.
            _file: f,
        }));
        const placeholderByFile = new Map(picked.map((f, i) => [f, placeholders[i].id]));
        setAttachments(prev => [...prev, ...placeholders]);
        setAttachError('');

        for (const file of picked) {
            const phId = placeholderByFile.get(file);
            try {
                const data = await uploadAttachmentFile(file);
                const ready = _readyRecordFromResponse(data, file, phId);
                setAttachments(prev => prev.map(a => (
                    a.id === phId ? { ...ready, _file: file } : a
                )));
            } catch (err) {
                const reason = err.detail || err.message || 'network error';
                setAttachments(prev => prev.map(a =>
                    a.id === phId
                        ? { ...a, status: 'error', progress: 100, parsed_text: '', block_reason: reason, _file: file }
                        : a
                ));
                setAttachError(`"${file.name}" could not be processed: ${reason}`);
            }
        }
    }, [uploadAttachmentFile, _readyRecordFromResponse]);

    // Re-upload an existing (errored or ready) attachment with `force_ocr=true`.
    // Used by the 🔄 button on errored chips so the user has a single-click
    // fix when the auto-selected engine missed text. Keeps the same chip id
    // so the user doesn't see it disappear and reappear.
    const retryAttachmentWithOcr = useCallback(async (att) => {
        const file = att && att._file;
        if (!file) {
            setAttachError(`Cannot retry "${att?.file_name || 'file'}" — original bytes are no longer available, please re-attach.`);
            return;
        }
        setAttachments(prev => prev.map(a =>
            a.id === att.id ? { ...a, status: 'uploading', progress: 0, block_reason: '' } : a
        ));
        try {
            const data = await uploadAttachmentFile(file, { forceOcr: true });
            const ready = _readyRecordFromResponse(data, file, att.id);
            setAttachments(prev => prev.map(a =>
                a.id === att.id ? { ...ready, _file: file } : a
            ));
            setAttachError('');
        } catch (err) {
            const reason = err.detail || err.message || 'network error';
            setAttachments(prev => prev.map(a =>
                a.id === att.id ? { ...a, status: 'error', progress: 100, block_reason: reason, _file: file } : a
            ));
            setAttachError(`Retry failed for "${file.name}": ${reason}`);
        }
    }, [uploadAttachmentFile, _readyRecordFromResponse]);

    // Derived once per `attachments` change — the send-button disabled prop
    // and `handleSend` would otherwise re-filter the array on every keystroke.
    const readyAttachments = useMemo(
        () => attachments.filter(a => a.status === 'ready'),
        [attachments],
    );
    const hasUploadingAttachment = useMemo(
        () => attachments.some(a => a.status === 'uploading'),
        [attachments],
    );
    // Filenames the user UPLOADED across this thread's messages. Passed to
    // sniffGeneratedFiles so an uploaded input file echoed in the assistant's
    // prose (e.g. "Summary of Report.xlsx") is never rendered as a generated
    // download card — those point at a /generated-files/ path that doesn't
    // exist and surface a dead "file has expired" link. Sourced from the
    // per-message `attachments` arrays set on both live send (file_name) and
    // history reload (mapHistoryToUiMessages reconstructs file_name too).
    const uploadedAttachmentNames = useMemo(() => {
        const names = new Set();
        for (const m of messages) {
            if (m?.type === 'user' && Array.isArray(m.attachments)) {
                for (const att of m.attachments) {
                    if (att?.file_name) names.add(att.file_name.toLowerCase());
                }
            }
        }
        return names;
    }, [messages]);

    const [kbDocumentNames, setKbDocumentNames] = useState(new Set());
    const kbDocumentScope = useMemo(() => {
        const namespaces = new Set();
        const docIds = new Set();
        for (const n of nodes) {
            const knowledge = n?.type === 'agent' ? n.data?.knowledge : null;
            if (!knowledge || knowledge.mode === 'none') continue;
            for (const ns of knowledge.namespaces || []) {
                if (ns) namespaces.add(String(ns));
            }
            for (const id of [
                ...(knowledge.selected_doc_ids || []),
                ...(knowledge.full_file_doc_ids || []),
                ...(knowledge.uploaded_doc_ids || []),
            ]) {
                if (id) docIds.add(String(id));
            }
        }
        return {
            namespaces: Array.from(namespaces).sort(),
            docIds: Array.from(docIds).sort(),
        };
    }, [nodes]);

    const kbDocumentScopeKey = useMemo(
        () => JSON.stringify(kbDocumentScope),
        [kbDocumentScope],
    );

    useEffect(() => {
        const { namespaces = [], docIds = [] } = JSON.parse(kbDocumentScopeKey);
        const hasKbScope = namespaces.length > 0 || docIds.length > 0;
        if (!hasKbScope) {
            setKbDocumentNames(new Set());
            return;
        }
        let cancelled = false;
        (async () => {
            try {
                const res = await kbFetch('?status=ACTIVE&limit=10000');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (cancelled) return;
                const namespaceSet = new Set(namespaces);
                const docIdSet = new Set(docIds);
                const names = new Set();
                for (const doc of data.docs || []) {
                    const id = doc?.id != null ? String(doc.id) : '';
                    const ns = doc?.namespace != null ? String(doc.namespace) : '';
                    if (docIdSet.has(id) || namespaceSet.has(ns)) {
                        const name = doc.name || doc.filename || doc.original_filename;
                        if (name) names.add(String(name).toLowerCase());
                    }
                }
                setKbDocumentNames(names);
            } catch (err) {
                if (!cancelled) setKbDocumentNames(new Set());
            }
        })();
        return () => { cancelled = true; };
    }, [kbDocumentScopeKey]);

    const generatedFileExcludeNames = useMemo(() => {
        const names = new Set(uploadedAttachmentNames);
        for (const name of kbDocumentNames) names.add(name);
        return names;
    }, [uploadedAttachmentNames, kbDocumentNames]);
    // Upload progress is shown as an indeterminate animation (no % number) —
    // fetch() cannot expose real upload bytes, and for scanned PDFs the bulk
    // of the wait is server-side OCR which is opaque to the browser.

// Format: "[File: x]\n<parsed>\n\n[Image asset: y]\nUse the file at ...\n\nUser question: <text>".
    // Skips blocked / errored / empty docs. Returns plain text when no files.
    const buildPromptWithAttachments = useCallback((records, text) => {
        const blocks = records
            .filter(a => a.status === 'ready')
            .map(a => {
                if (a.kind === 'image') {
                    const desc = a.parsed_text ? `\nDescription: ${a.parsed_text}` : '';
                    const pathHint = a.asset_path
                        ? `Load the image from this absolute path: "${a.asset_path}"`
                        : `Reference this image by the filename "${a.asset_name}" (it is located in the directory pointed to by the GENERATED_FILES_DIR environment variable).`;
                    return `[Image asset: ${a.file_name}]\n${pathHint}${desc}\nDownload URL: ${a.download_url || '(unavailable)'}`;
                }
                if (!a.parsed_text) return null;
                const slice = a.parsed_text.slice(0, CHAT_ATTACH_PROMPT_BUDGET_CHARS);
                const wasClipped = a.parsed_text.length > CHAT_ATTACH_PROMPT_BUDGET_CHARS;
                const suffix = wasClipped
                    ? `\n[...truncated ${a.parsed_text.length - CHAT_ATTACH_PROMPT_BUDGET_CHARS} chars to fit context]`
                    : '';
                return `[File: ${a.file_name}]\n${slice}${suffix}`;
            })
            .filter(Boolean);
        if (blocks.length === 0) return text;
        return blocks.join('\n\n') + '\n\nUser question: ' + text;
    }, []);

    // ── Assistant message action-bar handlers ───────────────────────────────
    function handleCopyMessage(content, msgId) {
        const text = safeString(content);
        const write = navigator.clipboard
            ? navigator.clipboard.writeText(text)
            : Promise.resolve(copyTextToClipboard(text));
        write.then(() => {
            setCopiedMsgId(msgId);
            setTimeout(() => setCopiedMsgId(null), 1500);
        }).catch(() => {});
    }

    const handleShareMessage = (content) => shareMessage(safeString(content), 'Workflow Response');
    const handleTeamsShare   = (content) => shareToTeams(safeString(content));

    function handleRegenerate() {
        if (isExecuting) return;
        // Find the last user message and replay it through the workflow.
        const msgs = messages;
        let lastUserIdx = -1;
        for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].type === 'user') { lastUserIdx = i; break; }
        }
        if (lastUserIdx === -1) return;
        const lastUserMsg = msgs[lastUserIdx];
        const text = safeString(lastUserMsg.content);
        // Drop the last assistant reply (everything after the last user message)
        // so the regenerated response replaces it cleanly.
        setMessages(msgs.slice(0, lastUserIdx + 1));
        setMessage(text);
        // Let React flush the state reset, then trigger the normal send path.
        setTimeout(() => handleSend(), 0);
    }

    const handleSend = async () => {
        // Allow attachment-only sends (matches the sidebar chat affordance).
        const hasText = message.trim().length > 0;
        const hasReadyAttach = readyAttachments.length > 0;
        if ((!hasText && !hasReadyAttach) || isExecuting) return;
        // Refuse to send while uploads are still in flight so we don't drop
        // file content from the prompt.
        if (hasUploadingAttachment) {
            setAttachError('Wait for attachments to finish uploading');
            return;
        }
        const activeThreadId = threadId || createThreadId(workflowId);
        if (!threadId) {
            setThreadId(activeThreadId);
        }

        // Validate workflow
        const validation = isWorkflowValid();
        if (!validation.valid) {
            const ts = Date.now();
            setMessages(prev => [...prev,
            { id: `user-${ts}`, type: 'user', content: message },
            { id: `err-${ts}`, type: 'error', content: validation.error }
            ]);
            setMessage('');
            return;
        }

        const displayQuery = hasText ? message : `(sent ${readyAttachments.length} attachment${readyAttachments.length === 1 ? '' : 's'})`;
        const baseText = hasText ? message : 'Please review the attached document(s).';
        // Documents now travel as a structured `attachments` array on the
        // /run-stream body (see below) so the engine can inject them into
        // agents size-aware (small → first agent only; big → every agent).
        // The user_input carries ONLY the typed prose — no more gluing the
        // parsed text into the prompt string (which caused the document to
        // degrade agent-to-agent). `sentQuery` is kept for the HITL resume
        // fallback path, which still passes free-form text via human_input.
        const runAttachments = readyAttachments
            .filter(a => a.status === 'ready' && a.parsed_text)
            .map(a => ({
                file_name: a.file_name,
                file_type: a.file_type,
                parsed_text: a.parsed_text,
                char_count: a.char_count || a.parsed_text.length,
                page_count: a.page_count || 0,
            }));
        // `user_input` is what the backend PERSISTS as the user ChatMessage
        // content (native_engine._save_user_prompt). The structured `attachments`
        // array (with parsed_text) is NOT persisted, so if we send only the bare
        // prose the filename is lost on reload and the attachment chip vanishes
        // from history (the reported bug). To make the chip survive reload
        // WITHOUT a backend change, prepend a NAME-ONLY `[File: <name>]` marker
        // to user_input in the exact shape parsePersistedUserPrompt expects. On
        // reload that marker is stripped back into a clean chip + typed text.
        // The document CONTENT still reaches the LLM via the structured
        // `attachments` body field below, so this marker adds only the filename
        // (which the model already sees) — no semantic change to the run.
        // Include restored chips (no parsed_text, _restored flag) in the
        // filename marker so the persisted user_input still carries the
        // [File: name] prefix — this makes the chip survive a page reload
        // even for resumed runs where the content lives in backend state.
        const readyForMarker = readyAttachments.filter(a => a.status === 'ready');
        const sentQuery = readyForMarker.length > 0
            ? `${readyForMarker.map(a => `[File: ${a.file_name}]`).join('\n\n')}\n\nUser question: ${baseText}`
            : baseText;

        // Attachments are rendered as structured chips inside the user
        // bubble (see the .message-bubble.user-bubble branch below). We keep
        // the prose part — `displayQuery` — clean and put the file metadata
        // on a sibling `attachments` field so the UI can lay them out with
        // an icon, filename, and size instead of an inline "📎 foo.md" line.
        // The full parsed_text still travels to the backend via `sentQuery`.
        const messageAttachments = readyAttachments.map(a => ({
            file_name: a.file_name,
            file_type: a.file_type,
            file_size: a.file_size,
        }));
        setMessages(prev => [...prev, {
            id: `user-${Date.now()}`,
            type: 'user',
            content: displayQuery,
            attachments: messageAttachments,
        }]);
        const userQuery = sentQuery;
        setMessage('');
        setAttachments([]);
        setAttachError('');

        // Start execution
        setExecuting(true);
        clearExecutionLogs();
        setExecutionError(null);
        setExecutionResult(null);
        setStreamingContent('');
        setStreamingAgent('');
        setFallbackStatus(null);
        let shouldReloadThreads = false;

        // Track run-level metadata for the assistant message action bar.
        const runStartTime = Date.now();
        let runModel = '';

        const controller = new AbortController();
        abortRef.current = controller;

        // Declared outside the try so the `finally` below can read it — a `let`
        // inside the try block is not in scope there.
        let handedOffToResume = false;

        try {
            // If a resume snapshot is pending (user_cancelled or node_failed),
            // route this message through /resume-stream so the engine continues
            // from the paused node instead of restarting the workflow. The user's
            // typed text is passed as human_input; the resume branch on the
            // backend accepts free-form text for user_cancelled the same way it
            // does for edit/reject on other reasons.
            if (failureSnapshot && failureSnapshot.threadId === activeThreadId) {
                const pendingSnap = failureSnapshot;
                setFailureSnapshot(null);
                setExecuting(false);   // handleHitlSubmit will flip this back on
                // Resume path: the paused run's snapshot already carries the
                // uploaded documents on server-side state, so downstream agents
                // keep them. For safety with pre-feature snapshots we still
                // inline the parsed text into the free-form human_input here.
                const resumeText = buildPromptWithAttachments(readyAttachments, baseText);
                await handleHitlSubmit(resumeText, {
                    threadId: pendingSnap.threadId,
                    interruptType: pendingSnap.errorType || 'node_failed',
                    agent: pendingSnap.agent || '',
                    prompt: '',
                    question: '',
                    options: [],
                });
                return;   // do NOT fall through to the normal /run-stream POST
            }

            const workflow = getWorkflowForExecution();
            // Connect directly to backend for streaming (bypasses Vite proxy buffering)
            const response = await fetch(`${API_BASE}/run-stream`, {
                method: 'POST',
                headers: buildAuthHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({
                    workflow,
                    user_input: userQuery,
                    // Structured uploaded documents — the engine seeds these
                    // onto run state and injects them into agents size-aware.
                    attachments: runAttachments,
                    workflow_id: workflowId,
                    workflow_name: workflowName,
                    thread_id: activeThreadId,
                    // Run-settings strip → workflow-wide swarm opt-in for
                    // this execute. Null/undefined would let the engine
                    // use its default; we always send the explicit user
                    // choice so behaviour is deterministic.
                    subagents_enabled: runSubagentsEnabled,
                }),
                signal: controller.signal,
            });

            if (!response.ok) {
                // Parse the JSON error body so structured {detail:{code,message}}
                // (e.g. budget errors) surfaces a clean message instead of a raw
                // JSON blob. Fall back to a status line for non-JSON bodies.
                let body = null;
                try { body = await response.json(); } catch { /* non-JSON error body */ }
                const msg = body
                    ? errText(body.detail, body.message, `Server error (${response.status})`)
                    : `Server error (${response.status})`;
                throw new Error(msg);
            }
            shouldReloadThreads = true;

            const streamContentType = response.headers.get('Content-Type') || '';
            // Validate content-type and body type before consuming the stream.
            // This ensures response.body is only accessed after confirming it is
            // a trusted SSE stream from our own API endpoint.
            if (!streamContentType.includes('text/event-stream')) {
                throw new Error('Unexpected response type from server');
            }
            // Real validation gate (not a rename) — rejects any object that
            // isn't an actual ReadableStream before a single byte is read
            // from it (CWE-79 hardening; see getValidatedStreamBody above).
            const validatedBody = getValidatedStreamBody(response);
            if (!validatedBody) throw new Error('No response body');
            const reader = validatedBody.getReader();
            const decoder = new TextDecoder();
            let currentAgentResponse = '';
            let currentAgent = '';
            let sseBuffer = '';
            let hitlInterrupted = false;
            // NOTE: `handedOffToResume` is declared above the try block, since
            // the `finally` needs to read it. It is set when this stream hands
            // control to a /resume-stream call (the "Allow all this session"
            // auto-approve path). The resume owns `abortRef` and the executing
            // flag from that moment on, so this stream's `finally` must not
            // reset them — doing so nulls the live resume's AbortController
            // (breaking Stop) and hides the running indicator while tokens are
            // still arriving.
            // Debug Log per-node accumulator. The backend does NOT report
            // LLM usage in its SSE stream today, so we approximate token
            // usage from the visible input + output text length using the
            // industry rule of thumb `~1 token per 4 chars` (English).
            // This under-counts the true bill (misses system prompt, tool
            // definitions, intermediate tool-calling turns) but is a much
            // better order-of-magnitude signal than counting streamed
            // chunks — a workflow that consumes a 2000-char document and
            // produces a 1980-char summary now shows ~1000 tokens instead
            // of the misleading "21 chunks emitted".
            //
            // Shape: { [nodeId]: { agent, input, output, inputChars,
            //                       outputChars, tokensEstimate, chunksStreamed } }
            //
            // `tokensEstimate` is what the workflow-total row + per-node
            // detail badge use. `chunksStreamed` is kept as a secondary
            // signal in View JSON (still useful to see whether streaming
            // actually happened) but is no longer surfaced as "tokens".
            const nodeRunStats = {};
            const estimateTokens = (chars) => Math.max(0, Math.round((chars || 0) / 4));
            const recomputeStats = (stats) => {
                const inputChars  = (stats.input  || '').length;
                const outputChars = (stats.output || '').length;
                return {
                    inputChars,
                    outputChars,
                    tokensEstimate: estimateTokens(inputChars + outputChars),
                };
            };
            const trackNodeStat = (nodeId, patch) => {
                if (!nodeId) return;
                const prev = nodeRunStats[nodeId] || { chunksStreamed: 0 };
                const merged = { ...prev, ...patch };
                nodeRunStats[nodeId] = { ...merged, ...recomputeStats(merged) };
            };
            // `current_input` is what the engine pipes from the previous
            // node to the next. We seed it with the user's chat message
            // so the FIRST node's input row is non-empty.
            let pipedInput = displayQuery;

            while (true) {
                const { done, value } = await reader.read();
                if (done) {
                    sseBuffer += decoder.decode();
                } else {
                    sseBuffer += decoder.decode(value, { stream: true });
                }

                const rawEvents = sseBuffer.split('\n\n');
                sseBuffer = rawEvents.pop() || '';
                if (done && sseBuffer.trim()) {
                    rawEvents.push(sseBuffer);
                    sseBuffer = '';
                }

                for (const rawEvent of rawEvents) {
                    const normalized = rawEvent.replace(/\r/g, '');
                    const dataLine = normalized
                        .split('\n')
                        .find((evtLine) => evtLine.startsWith('data: '));

                    if (!dataLine) continue;

                    try {
                        const data = JSON.parse(dataLine.slice(6));

                        if (data.event === 'start') {
                            if (data?.data?.thread_id) {
                                setThreadId(data.data.thread_id);
                            }
                            // Mint a fresh debug-log run context. This is the
                            // earliest signal of "a new run is starting", so
                            // any prior rows from the previous run are dropped
                            // and a new runId is published. We seed
                            // currentInput here with the user's actual chat
                            // message — the backend never echoes the input
                            // back in SSE, so this is the only place we can
                            // capture it for the Session Context tab.
                            beginRunContext({
                                runId: data.data?.thread_id || undefined,
                                startedAt: new Date().toISOString(),
                                currentInput: displayQuery,
                            });
                            // Story-style bookend rows: Input → Start → ...
                            // → End → Output. These three (Input, Start) at
                            // the top of every run; End + Output get pushed
                            // on the `complete` event below.
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                group: 'Flow Initialization',
                                kind: 'Input',
                                nodeLabel: 'Input',
                                title: displayQuery || '(empty input)',
                                status: 'done',
                                raw: { event: 'user_input', data: { text: displayQuery } },
                            });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                group: 'Flow Initialization',
                                kind: 'Start',
                                nodeLabel: 'Start',
                                title: 'Workflow execution started',
                                status: 'done',
                                raw: data,
                            });
                        } else if (data.event === 'agent_start') {
                            currentAgent = data.data.agent;
                            currentAgentResponse = '';
                            setStreamingAgent(currentAgent);
                            setStreamingContent('');
                            // Stamp ``nodeId`` on the timeline row so
                            // ``buildAgentTimeline`` can slot subagent pills
                            // that carry the same nodeId directly beneath
                            // this agent instead of appending them to the
                            // flat step list (which mis-attributed them to
                            // whichever agent happened to render last).
                            addExecutionLog({
                                type: 'agent_start',
                                agent: currentAgent,
                                nodeId: data.data?.node_id || null,
                            });
                            const activeNode = findNodeForExecutionEvent(data.data);
                            if (activeNode) setNodeActive(activeNode.id);
                            // Stamp this node's input + agent name on the
                            // accumulator. We use whatever was last piped
                            // forward (`pipedInput`); the engine doesn't
                            // expose per-node inputs in SSE, so this is
                            // best-effort and clearly labelled in the JSON.
                            const startNodeId = data.data?.node_id || activeNode?.id || null;
                            trackNodeStat(startNodeId, {
                                agent: currentAgent,
                                input: pipedInput,
                                output: '',
                                chunksStreamed: 0,
                            });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                group: 'Flow Initialization',
                                nodeId: startNodeId,
                                title: 'Node initiated',
                                status: 'running',
                                raw: data,
                            });
                        } else if (data.event === 'agent_progress') {
                            // Intermediate agents emit agent_progress instead of
                            // agent_start/complete (only the terminal agent streams
                            // tokens to the user). Map them so the thinking timeline
                            // shows every agent in the workflow, not just the last.
                            const progressAgent = data.data.agent;
                            if (data.data.status === 'running') {
                                setStreamingAgent(progressAgent);
                                const node = findNodeForExecutionEvent(data.data);
                                addExecutionLog({
                                    type: 'agent_start',
                                    agent: progressAgent,
                                    nodeId: data.data?.node_id || node?.id || null,
                                });
                                if (node) setNodeActive(node.id);
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    group: 'Flow Initialization',
                                    nodeId: data.data?.node_id || node?.id || null,
                                    title: 'Node initiated',
                                    status: 'running',
                                    raw: data,
                                });
                            } else if (data.data.status === 'done') {
                                addExecutionLog({
                                    type: 'agent_complete',
                                    agent: progressAgent,
                                    nodeId: data.data?.node_id || null,
                                });
                                // Node finished — drop its transient retry/fallback
                                // status so it doesn't bleed onto the next node.
                                setFallbackStatus(null);
                                const node = findNodeForExecutionEvent(data.data);
                                if (node) clearNodeActive(node.id);
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    nodeId: data.data?.node_id || node?.id || null,
                                    title: 'Node processing is completed',
                                    status: 'done',
                                    raw: data,
                                });
                            }
                        } else if (data.event === 'agent_token') {
                            // Strip control characters from untrusted streamed
                            // model/tool output before it enters app state and
                            // is rendered as markdown (CWE-79 / Reflected XSS
                            // hardening; see sanitizeStreamToken above).
                            currentAgentResponse += sanitizeStreamToken(data.data.token);
                            setStreamingContent(currentAgentResponse);
                            // Count SSE chunks as a secondary signal (kept
                            // in View JSON so the user can see whether
                            // streaming actually happened) but do NOT
                            // treat it as a token estimate — chunk count
                            // ignores input + prompt entirely. Real
                            // per-node tokens are estimated from input +
                            // output text length at agent_complete time.
                            const tokNodeId = data.data?.node_id || null;
                            if (tokNodeId) {
                                const prev = nodeRunStats[tokNodeId] || { chunksStreamed: 0 };
                                nodeRunStats[tokNodeId] = {
                                    ...prev,
                                    chunksStreamed: (prev.chunksStreamed || 0) + 1,
                                };
                            }
                        } else if (data.event === 'agent_retry') {
                            handleRetryNotice(data, { setFallbackStatus, pushDebugRow });
                        } else if (data.event === 'agent_fallback') {
                            handleFallbackNotice(data, { setFallbackStatus, pushDebugRow });
                        } else if (data.event === 'agent_complete') {
                            const completedNode = findNodeForExecutionEvent(data.data);
                            if (completedNode) clearNodeActive(completedNode.id);
                            // Node finished — drop its transient retry/fallback
                            // status so it doesn't bleed onto the next node.
                            setFallbackStatus(null);
                            // Track the model used by the terminal agent for
                            // the message action bar metadata display.
                            if (data.data?.model) runModel = data.data.model;
                            addExecutionLog({
                                type: 'agent_complete',
                                agent: data.data.agent,
                                nodeId: data.data?.node_id || completedNode?.id || null,
                                output: data.data.output,
                                generatedFiles: data.data.generated_files || [],
                            });
                            const completedNodeId = data.data?.node_id || completedNode?.id || null;
                            const out = typeof data.data?.output === 'string' ? data.data.output : '';
                            trackNodeStat(completedNodeId, { output: out });
                            // The engine pipes the completed node's
                            // output to the next node's input, so update
                            // pipedInput for the next agent_start.
                            if (out) pipedInput = out;
                            const stats = nodeRunStats[completedNodeId] || {};
                            // Build a richer "raw" payload that includes
                            // per-node input/output chars + char-based
                            // token estimate. View JSON exposes every
                            // number so the user can sanity-check the
                            // estimate against the visible text. The
                            // original engine payload is preserved under
                            // `engine_event`.
                            const enrichedRaw = {
                                event: 'agent_complete',
                                node_id: completedNodeId,
                                agent: data.data?.agent,
                                input: stats.input || '',
                                output: out,
                                input_chars: stats.inputChars || 0,
                                output_chars: stats.outputChars || 0,
                                tokens_estimate: stats.tokensEstimate || 0,
                                tokens_estimate_note:
                                    'Char-based estimate (chars/4). Under-counts real usage — '
                                    + 'ignores system prompt + tool definitions + intermediate '
                                    + 'tool-calling turns.',
                                chunks_streamed: stats.chunksStreamed || 0,
                                usage: data.data?.usage || null,
                                model: data.data?.model || '',
                                generated_files: data.data?.generated_files || [],
                                engine_event: data,
                            };
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: completedNodeId,
                                title: 'Agent execution successful',
                                detail: usageSummaryText(data.data?.usage) || ((stats.tokensEstimate || 0) > 0
                                    ? `~${stats.tokensEstimate.toLocaleString()} tokens · `
                                      + `${(stats.inputChars || 0).toLocaleString()} chars in / `
                                      + `${(stats.outputChars || 0).toLocaleString()} chars out`
                                    : ''),
                                status: 'done',
                                generatedFiles: data.data?.generated_files || null,
                                raw: enrichedRaw,
                            });
                        } else if (data.event === 'complete') {
                            setStreamingContent('');
                            setStreamingAgent('');
                            setFallbackStatus(null);
                            // Capture the final per-run trace / output for
                            // the Debug Log's "Session Context" tab.
                            // The backend always emits `complete` after a
                            // run, even if the previous event was `error` —
                            // check the run status BEFORE setting it from
                            // `complete` so we can decide whether to render
                            // a "Run completed → SUCCESS" debug row or a
                            // "Run finished with errors" row instead.
                            const priorStatus = useWorkflowStore.getState().runContext.status;
                            // A ``hitl_rejected`` complete event means the
                            // reviewer pressed Reject on the HITL card. Render
                            // it as a distinct stop notice so the user can
                            // tell the run was deliberately ended rather than
                            // simply finished.
                            const rejected = !!data.data?.hitl_rejected;
                            setRunContextFromComplete(data.data || {});
                            const finalOutputText = safeString(data.data?.output) || '';
                            // Story-style bookend rows — every run ends
                            // with an explicit "End" row, then an "Output"
                            // row showing what the assistant produced. On
                            // an errored / rejected run we skip the Output
                            // row because the engine echoes the error
                            // string into `data.output` and we don't want
                            // to mislead the user by labelling that as
                            // "Output". The Error row above already
                            // surfaces the failure.
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                kind: 'End',
                                nodeLabel: 'End',
                                title: priorStatus === 'error'
                                    ? 'Workflow ended with errors'
                                    : (rejected ? 'Workflow stopped by reviewer' : 'Workflow execution finished'),
                                status: priorStatus === 'error' || rejected ? 'error' : 'done',
                                raw: data,
                            });
                            if (priorStatus !== 'error' && !rejected) {
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    kind: 'Output',
                                    nodeLabel: 'Output',
                                    title: finalOutputText || '(no output)',
                                    status: 'done',
                                    raw: { event: 'final_output', data: { text: finalOutputText } },
                                });
                            }
                            {
                                const usage = data.data?.usage || null;
                                const perNode = nodeRunStats || {};
                                const totals = Object.values(perNode).reduce(
                                    (acc, s) => ({
                                        inputChars:  acc.inputChars  + (s.inputChars  || 0),
                                        outputChars: acc.outputChars + (s.outputChars || 0),
                                        chunksStreamed: acc.chunksStreamed + (s.chunksStreamed || 0),
                                    }),
                                    { inputChars: 0, outputChars: 0, chunksStreamed: 0 },
                                );
                                const totalChars = totals.inputChars + totals.outputChars;
                                const totalTokensEstimate = estimateTokens(totalChars);
                                const nodeCount = Object.keys(perNode).length;
                                const hasBackendUsage = !!usage && (
                                    Number(usage.total_tokens || 0) > 0
                                    || Number(usage.prompt_tokens || 0) > 0
                                    || Number(usage.completion_tokens || 0) > 0
                                    || Number(usage.cost_usd || 0) > 0
                                );
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    kind: 'Tokens',
                                    nodeLabel: hasBackendUsage ? 'Usage' : 'Tokens (approx)',
                                    title: hasBackendUsage
                                        ? `${formatUsageValue(usage.total_tokens || ((usage.prompt_tokens || 0) + (usage.completion_tokens || 0)))} tokens${usage.estimated ? ' (estimated)' : ''} · ${formatCostUsd(usage.cost_usd)}`
                                        : `~${totalTokensEstimate.toLocaleString()} tokens (est. from ${totalChars.toLocaleString()} chars in/out across ${nodeCount} node${nodeCount === 1 ? '' : 's'})`,
                                    detail: hasBackendUsage
                                        ? `${formatUsageValue(usage.prompt_tokens)} prompt · ${formatUsageValue(usage.completion_tokens)} completion`
                                        : `${totals.inputChars.toLocaleString()} chars in · ${totals.outputChars.toLocaleString()} chars out · ${totals.chunksStreamed.toLocaleString()} SSE chunks streamed`,
                                    status: 'done',
                                    raw: {
                                        event: 'token_usage_summary',
                                        usage,
                                        total_tokens_estimate: totalTokensEstimate,
                                        total_input_chars:  totals.inputChars,
                                        total_output_chars: totals.outputChars,
                                        total_chars:        totalChars,
                                        total_chunks_streamed: totals.chunksStreamed,
                                        node_count: nodeCount,
                                        per_node: perNode,
                                    },
                                });
                            }
                            const output = safeString(data.data.output) || 'No output received';
                            setExecutionResult(output);
                            const allFiles = (data.data.generated_files || []);
                            const runDurationS = runStartTime
                                ? Math.round((Date.now() - runStartTime) / 1000)
                                : null;
                            setMessages(prev => [...prev, {
                                type: rejected ? 'hitl-rejected' : 'assistant',
                                content: rejected
                                    ? 'Run stopped — reviewer rejected the output. Adjust the agent\u2019s instructions or input and try again.'
                                    : output,
                                trace: data.data.execution_trace,
                                generatedFiles: allFiles,
                                usage: data.data?.usage || null,
                                model: runModel || '',
                                durationS: runDurationS,
                            }]);
                            // Unblock the input immediately — history save happens after
                            // this event on the backend and may take extra time.
                            setExecuting(false);
                        } else if (data.event === 'error') {
                            setStreamingContent('');
                            setStreamingAgent('');
                            setFallbackStatus(null);
                            // Backend budget errors send data.detail as a
                            // {code, message} object — unwrap to a clean string
                            // so the error card doesn't show "[object Object]".
                            const errMsg = errText(data.data.detail, data.data.message, 'Unknown error occurred');
                            setExecutionError(errMsg);
                            setMessages(prev => [...prev, { type: 'error', content: errMsg }]);
                            setRunStatus('error');
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                title: 'Error',
                                detail: errMsg,
                                status: 'error',
                                raw: data,
                            });
                            // Fallback trigger for the retry banner: the
                            // /chat-pending poll after an error can be racy
                            // (snapshot may not be persisted yet), so if the
                            // backend flagged this failure as retryable we
                            // populate the snapshot directly from the event
                            // AND kick off a retry-backed hydration so the
                            // full snapshot metadata (completed_nodes,
                            // last_input, error_type) fills in as soon as
                            // the DB row is written.
                            if (data.data?.retryable === true && data.data?.node_id) {
                                setFailureSnapshot({
                                    threadId: threadId,
                                    nodeId: data.data.node_id,
                                    agent: data.data.agent || '',
                                    error: data.data.message || 'Node failed',
                                    errorType: '',
                                    completedNodes: [],
                                    lastInput: '',
                                });
                            }
                            // Always follow up with a retry-backed poll —
                            // covers both the retryable case above (fills
                            // in richer metadata) and the case where the
                            // error event lacks retryable but the backend
                            // still persisted a snapshot (e.g. subflow
                            // failure paths that haven't been threaded
                            // through the retryable flag yet).
                            hydrateFailureSnapshotWithRetry(threadId);
                        } else if (data.event === 'tool_call_start') {
                            addExecutionLog({
                                type: 'tool_call',
                                agent: data.data.agent,
                                tool: data.data.tool_name,
                                args: data.data.arguments,
                            });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: `tool:${data.data?.tool_name || 'tool'}:${data.data?.agent || ''}`,
                                kind: 'Tool',
                                nodeLabel: data.data?.tool_name || 'Tool',
                                title: `Invoked by ${data.data?.agent || 'agent'}`,
                                status: 'running',
                                raw: {
                                    event: 'tool_call_start',
                                    tool_name: data.data?.tool_name,
                                    node_id: data.data?.node_id,
                                    agent: data.data?.agent,
                                    input: data.data?.arguments ?? null,
                                    engine_event: data,
                                },
                            });
                            // For delegation tool calls we let the dedicated
                            // subagent_start event own the streaming label —
                            // showing the synthetic delegate_to_<alias> name
                            // here would leak an internal identifier.
                            if (typeof data.data.tool_name === 'string'
                                && data.data.tool_name.startsWith('delegate_to_')) {
                                const alias = data.data.tool_name.slice('delegate_to_'.length);
                                setStreamingAgent(`Delegating this task to the ${alias} subagent…`);
                            } else {
                                setStreamingAgent(`${data.data.agent} -> ${data.data.tool_name}`);
                            }
                        } else if (data.event === 'tool_call_result') {
                            addExecutionLog({
                                type: 'tool_result',
                                agent: data.data.agent,
                                tool: data.data.tool_name,
                                result: data.data.result,
                            });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: `tool:${data.data?.tool_name || 'tool'}:${data.data?.agent || ''}`,
                                kind: 'Tool',
                                nodeLabel: data.data?.tool_name || 'Tool',
                                title: 'Tool returned',
                                status: 'done',
                                raw: {
                                    event: 'tool_call_result',
                                    tool_name: data.data?.tool_name,
                                    node_id: data.data?.node_id,
                                    agent: data.data?.agent,
                                    output: data.data?.result ?? null,
                                    engine_event: data,
                                },
                            });
                            setStreamingAgent(`${data.data.agent} working...`);
                        } else if (data.event === 'swarm_plan') {
                            // Planner finished — N workers about to spawn.
                            // Render planning pills immediately so the user
                            // sees role names before the first subagent_start
                            // event arrives (workers can take 1-10s to start).
                            addExecutionLog({
                                type:    'swarm_plan',
                                runId:   data.data?.run_id,
                                nodeId:  data.data?.node_id || null,
                                strategy: data.data?.strategy,
                                roleIds: data.data?.role_ids || [],
                                workerCount: data.data?.worker_count || 0,
                            });
                            const n = data.data?.worker_count || 0;
                            if (n > 0) {
                                setStreamingAgent(`Planning ${n} sub-agent${n === 1 ? '' : 's'}…`);
                            }
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                kind: 'Swarm',
                                nodeLabel: 'Swarm planner',
                                title: `Planned ${n} sub-agent${n === 1 ? '' : 's'}`,
                                detail: (data.data?.role_ids || []).join(', '),
                                status: 'done',
                                raw: data,
                            });
                        } else if (data.event === 'swarm_error') {
                            // Swarm couldn't run (plan_validation_failed,
                            // orchestrator_failure, manifest_failure). Surface
                            // as a failed pill so the user sees WHY instead of
                            // waiting for the parent LLM's paraphrase.
                            addExecutionLog({
                                type:   'swarm_error',
                                runId:  data.data?.run_id,
                                nodeId: data.data?.node_id || null,
                                code:   data.data?.code || 'swarm_error',
                                detail: data.data?.detail || '',
                            });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                kind: 'Swarm',
                                nodeLabel: 'Swarm planner',
                                title: `Swarm error: ${data.data?.code || 'unknown'}`,
                                detail: data.data?.detail || '',
                                status: 'error',
                                raw: data,
                            });
                        } else if (data.event === 'kb_retrieval') {
                            // RAG retrieval ran for this node. Surface WHICH
                            // chunks qualified, each chunk's source + full
                            // text + per-chunk score (n/a — not exposed by the
                            // platform retriever), and the run-level
                            // confidence. Emitted even when 0 chunks matched so
                            // the operator can see retrieval ran and returned
                            // nothing.
                            const kbChunks = Array.isArray(data.data?.chunks) ? data.data.chunks : [];
                            const kbCount = data.data?.chunk_count ?? kbChunks.length;
                            const kbConf = data.data?.confidence;
                            const confStr = (kbConf === null || kbConf === undefined)
                                ? '' : ` · confidence ${Number(kbConf).toFixed(2)}`;
                            addExecutionLog({
                                type:       'kb_retrieval',
                                nodeId:     data.data?.node_id || null,
                                agent:      data.data?.agent,
                                mode:       data.data?.mode,
                                query:      data.data?.query,
                                chunkCount: kbCount,
                                confidence: kbConf ?? null,
                                chunks:     kbChunks,
                            });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: data.data?.node_id || null,
                                kind: 'Knowledge',
                                nodeLabel: `Knowledge retrieval${data.data?.agent ? ` · ${data.data.agent}` : ''}`,
                                title: kbCount > 0
                                    ? `${kbCount} chunk${kbCount === 1 ? '' : 's'} qualified${confStr}`
                                    : `No chunks matched${confStr}`,
                                detail: data.data?.query ? `query: ${data.data.query}` : '',
                                status: 'done',
                                raw: {
                                    event: 'kb_retrieval',
                                    node_id: data.data?.node_id,
                                    agent: data.data?.agent,
                                    mode: data.data?.mode,
                                    query: data.data?.query,
                                    chunk_count: kbCount,
                                    confidence: kbConf ?? null,
                                    chunks: kbChunks,
                                    engine_event: data,
                                },
                            });
                        } else if (data.event === 'subagent_start') {
                            // A sub-agent delegation just kicked off. Render
                            // a "Delegated to <alias>" pill in the timeline so
                            // the user can see who is doing what mid-thought.
                            //
                            // ``nodeId`` (workflow path only) lets
                            // ``buildAgentTimeline`` slot this pill directly
                            // beneath the parent agent node in the flat step
                            // list, so a workflow with two agents each
                            // spawning subagents groups them under the right
                            // node instead of piling every subagent under
                            // whichever agent happens to be "current" at the
                            // moment the frame arrives (the old bug).
                            addExecutionLog({
                                type: 'subagent_start',
                                callId:        data.data.call_id,
                                nodeId:        data.data.node_id || null,
                                alias:         data.data.alias,
                                agentId:       data.data.agent_id,
                                parentAgentId: data.data.parent_agent_id,
                                taskPreview:   data.data.task_preview,
                                tools:         Array.isArray(data.data.tools)  ? data.data.tools  : [],
                                skills:        Array.isArray(data.data.skills) ? data.data.skills : [],
                            });
                            setStreamingAgent(`Delegating this task to the ${data.data.alias} subagent…`);
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                // Use call_id as the "node" key so the matching
                                // subagent_complete can close this exact row.
                                nodeId: `subagent:${data.data?.call_id || data.data?.alias}`,
                                kind: 'Sub-agent',
                                nodeLabel: `Sub-agent: ${data.data?.alias || 'worker'}`,
                                title: `Delegated by ${data.data?.parent_agent_id || 'parent'}`,
                                // Full task is the entire input to the subagent;
                                // task_preview is only the 160-char pill text.
                                detail: data.data?.task || data.data?.task_preview || '',
                                status: 'running',
                                raw: {
                                    event: 'subagent_start',
                                    call_id: data.data?.call_id,
                                    alias: data.data?.alias,
                                    agent_id: data.data?.agent_id,
                                    parent_agent_id: data.data?.parent_agent_id,
                                    // Entire, untruncated subagent input.
                                    input: data.data?.task || data.data?.task_preview || '',
                                    task_preview: data.data?.task_preview || '',
                                    tools_available: Array.isArray(data.data?.tools) ? data.data.tools : [],
                                    skills_available: Array.isArray(data.data?.skills) ? data.data.skills : [],
                                    engine_event: data,
                                },
                            });
                        } else if (data.event === 'subagent_complete') {
                            addExecutionLog({
                                type: 'subagent_complete',
                                callId:        data.data.call_id,
                                nodeId:        data.data.node_id || null,
                                alias:         data.data.alias,
                                agentId:       data.data.agent_id,
                                parentAgentId: data.data.parent_agent_id,
                                durationS:     data.data.duration_s,
                                ok:            data.data.ok,
                                error:         data.data.error,
                                files:         data.data.files || [],
                                preview:       data.data.preview || '',
                            });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: `subagent:${data.data?.call_id || data.data?.alias}`,
                                kind: 'Sub-agent',
                                nodeLabel: `Sub-agent: ${data.data?.alias || 'worker'}`,
                                title: data.data?.ok
                                    ? `Completed in ${data.data?.duration_s ?? '?'}s`
                                    : `Failed: ${data.data?.error || 'unknown'}`,
                                // Full subagent output (falls back to the
                                // 240-char preview only if the engine didn't
                                // send the complete text).
                                detail: data.data?.output || data.data?.preview || '',
                                status: data.data?.ok ? 'done' : 'error',
                                generatedFiles: Array.isArray(data.data?.generated_files)
                                    ? data.data.generated_files
                                    : (Array.isArray(data.data?.files) ? data.data.files : null),
                                raw: {
                                    event: 'subagent_complete',
                                    call_id: data.data?.call_id,
                                    alias: data.data?.alias,
                                    agent_id: data.data?.agent_id,
                                    parent_agent_id: data.data?.parent_agent_id,
                                    // Entire, untruncated subagent output +
                                    // the JSON-parsed structured form.
                                    output: data.data?.output || data.data?.preview || '',
                                    output_payload: data.data?.output_payload ?? null,
                                    preview: data.data?.preview || '',
                                    ok: !!data.data?.ok,
                                    error: data.data?.error || null,
                                    duration_s: data.data?.duration_s,
                                    files: data.data?.generated_files || data.data?.files || [],
                                    engine_event: data,
                                },
                            });
                        } else if (data.event === 'condition_flash') {
                            const conditionNodeId = data.data.node_id;
                            setNodeActive(conditionNodeId);
                            setTimeout(() => clearNodeActive(conditionNodeId), 400);
                        } else if (data.event === 'condition_routed') {
                            addExecutionLog({
                                type: 'condition_routed',
                                nodeId: data.data?.node_id,
                                matchedCase: data.data?.matched_case,
                                matchedCaseLabel: data.data?.matched_case_label,
                                expression: data.data?.expression,
                                evaluated: data.data?.evaluated,
                                warning: data.data?.warning,
                            });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: data.data?.node_id || null,
                                title: `Condition routed → ${data.data?.matched_case_label || data.data?.matched_case || 'branch'}`,
                                detail: data.data?.expression || '',
                                status: 'done',
                                raw: data,
                            });
                        } else if (data.event === 'loop_iteration_start') {
                            const { node_id, mode, index, total } = data.data || {};
                            if (node_id) {
                                setLoopProgress(node_id, { running: true, index, total, mode });
                                addExecutionLog({
                                    type: 'loop_iter',
                                    nodeId: node_id,
                                    index, total, mode,
                                });
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    nodeId: node_id,
                                    title: `Loop iteration ${typeof index === 'number' ? index + 1 : '?'}${total ? `/${total}` : ''} started`,
                                    status: 'running',
                                    raw: data,
                                });
                            }
                        } else if (data.event === 'loop_iteration_end') {
                            // No visible UI change — we keep the badge on
                            // the current round until the next iteration_start
                            // (or loop_complete) replaces it.
                        } else if (data.event === 'loop_complete') {
                            const { node_id, total_iterations, max_iterations_hit } = data.data || {};
                            if (node_id) {
                                clearLoopProgress(node_id);
                                addExecutionLog({
                                    type: 'loop_done',
                                    nodeId: node_id,
                                    total: total_iterations,
                                    maxHit: !!max_iterations_hit,
                                });
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    nodeId: node_id,
                                    title: `Loop completed${total_iterations ? ` (${total_iterations} iterations)` : ''}`,
                                    detail: max_iterations_hit ? 'Max iterations hit' : '',
                                    status: 'done',
                                    raw: data,
                                });
                            }
                        } else if (data.event === 'loop_condition_eval') {
                            const { node_id, index, will_continue, case_results, eval_state, evaluator_pending } = data.data || {};
                            if (node_id) {
                                addExecutionLog({
                                    type: 'loop_condition',
                                    nodeId: node_id,
                                    index,
                                    willContinue: !!will_continue,
                                    caseResults: case_results || [],
                                    evalState: eval_state || null,
                                    evaluatorPending: !!evaluator_pending,
                                });
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    nodeId: node_id,
                                    title: `Loop condition evaluated → ${will_continue ? 'continue' : 'stop'}`,
                                    detail: typeof index === 'number' ? `iteration ${index + 1}` : '',
                                    status: 'done',
                                    raw: data,
                                });
                            }
                        } else if (data.event === 'loop_iteration_summary') {
                            handleLoopSummaryEvent(data.event, data.data, { addExecutionLog, setMessages });
                            const { node_id, index, score } = data.data || {};
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: node_id || null,
                                title: typeof score === 'number'
                                    ? `Iteration ${typeof index === 'number' ? index + 1 : '?'} scored ${score}`
                                    : `Iteration ${typeof index === 'number' ? index + 1 : '?'} summary`,
                                status: 'done',
                                raw: data,
                            });
                        } else if (data.event === 'loop_evaluation') {
                            handleLoopSummaryEvent(data.event, data.data, { addExecutionLog, setMessages });
                            const { node_id, index, evaluation, decision } = data.data || {};
                            const score = evaluation?.score;
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: node_id || null,
                                title: `LLM judge decision: ${decision?.action || 'eval'}`,
                                detail: typeof score === 'number'
                                    ? `iteration ${typeof index === 'number' ? index + 1 : '?'} — confidence ${score}`
                                    : '',
                                status: 'done',
                                raw: data,
                            });
                        } else if (data.event === 'loop_final_summary') {
                            handleLoopSummaryEvent(data.event, data.data, { addExecutionLog, setMessages });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                title: 'Loop final summary',
                                status: 'done',
                                raw: data,
                            });
                        } else if (handleLoopSummaryEvent(data.event, data.data, { addExecutionLog, setMessages })) {
                            // Catch-all for any future loop-summary subtype we
                            // forgot to wire above.
                        } else if (data.event === 'hitl_interrupt') {
                            // The backend snapshotted the run server-side. We
                            // build the UI-friendly hitlRequest shape the card
                            // already expects (`interruptType`, `question`,
                            // `options`, `prompt`, `agent`, `threadId`).
                            const payload = data.data || {};
                            const reason = payload.reason || 'after_response';
                            const threadIdFromEv = payload.thread_id;
                            let interruptType = reason;
                            let question = '';
                            let options = [];
                            let prompt = '';
                            if (reason === 'ask_human') {
                                interruptType = 'ask_human';
                                question = (payload.payload && payload.payload.question) || '';
                                options = ((payload.payload && payload.payload.options) || []);
                                prompt = question;
                            } else if (reason === 'before_tool') {
                                interruptType = 'before_tool';
                                const calls = payload.pending_tool_calls || [];
                                const first = calls[0] || {};
                                prompt = buildToolSummary(first.name, first.args || {});
                            } else {
                                interruptType = 'after_response';
                                prompt = payload.output || '';
                            }
                            // Session auto-approve short-circuit: user clicked
                            // "Allow all this session" earlier.
                            if (interruptType === 'before_tool' && sessionAutoApproveRef.current) {
                                // Hand off to the resume stream and stop
                                // reading this one. The backend has already
                                // closed its side at the pause, so there is
                                // nothing further to consume here; without the
                                // break both loops run concurrently and this
                                // one's `finally` tears down the resume's
                                // state the moment it observes `done`.
                                handedOffToResume = true;
                                hitlInterrupted = true;
                                handleHitlSubmit('APPROVED', {
                                    threadId: threadIdFromEv,
                                    interruptType, agent: payload.agent,
                                    prompt, question, options,
                                });
                            } else {
                                // Clear any partial streaming so the HITL
                                // card replaces the bubble cleanly. Without
                                // this, a `before_tool` pause that fires
                                // while the model was still typing leaves
                                // ghost text under the card.
                                setStreamingContent('');
                                setStreamingAgent('');
                                setFallbackStatus(null);
                                setHitlRequest({
                                    threadId: threadIdFromEv,
                                    interruptType,
                                    agent: payload.agent || 'Agent',
                                    nodeId: payload.node_id || '',
                                    prompt,
                                    question,
                                    options,
                                    pendingToolCalls: payload.pending_tool_calls || [],
                                });
                                setHitlRedirectText('');
                                setExecuting(false);
                                hitlInterrupted = true;
                            }
                            // Reason-specific HITL row so the user can tell at a
                            // glance WHY the run paused — reviewer approval,
                            // tool-call approval, or an explicit Ask-Human.
                            let hitlTitle = 'HITL: waiting for reviewer';
                            let hitlDetail = '';
                            if (reason === 'before_tool') {
                                const first = (payload.pending_tool_calls || [])[0] || {};
                                hitlTitle = `HITL: approve tool call — ${first.name || 'tool'}`;
                                hitlDetail = prompt || '';
                            } else if (reason === 'ask_human') {
                                hitlTitle = `HITL: human question`;
                                hitlDetail = question || '';
                            } else {
                                hitlTitle = 'HITL: review agent response';
                                hitlDetail = prompt || '';
                            }
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: payload.node_id || null,
                                nodeLabel: payload.agent || nodeLabelById[payload.node_id] || 'HITL',
                                kind: 'HITL',
                                title: hitlTitle,
                                detail: hitlDetail,
                                status: 'pending',
                                raw: data,
                            });
                        } else if (data.event === 'hitl_resumed') {
                            // Server acknowledged the resume — keep streaming.
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                kind: 'HITL',
                                nodeLabel: 'HITL',
                                title: 'Resumed with user input',
                                status: 'done',
                                raw: data,
                            });
                        } else if (data.event === 'workflow_retrying') {
                            // Informational — engine is about to re-run the
                            // failed node from scratch. Surface a debug row so
                            // the timeline shows the retry attempt.
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: data.data?.node_id || null,
                                title: `Retrying node`,
                                detail: `Retrying ${data.data?.agent || 'node'} after previous failure: ${data.data?.previous_error || ''}`.slice(0, 240),
                                status: 'info',
                                raw: data,
                            });
                        }
                    } catch (e) {
                        // Skip invalid JSON
                    }
                    if (hitlInterrupted) break;
                }

                if (done || hitlInterrupted) break;
            }
            // Release the reader lock on the (already server-closed) stream.
            // We exit this loop by `break` on a HITL pause rather than by
            // draining to `done`, so without this the lock is held until GC.
            if (hitlInterrupted) {
                try { await reader.cancel(); } catch { /* already closed */ }
            }
        } catch (error) {
            setStreamingContent('');
            setStreamingAgent('');
            setFallbackStatus(null);
            if (error.name !== 'AbortError') {
                setExecutionError(error.message);
                setMessages(prev => [...prev, {
                    type: 'error',
                    content: `Error: ${error.message}`
                }]);
            }
        } finally {
            // When we handed off to an in-flight /resume-stream, that call now
            // owns abortRef and the executing flag — resetting them here would
            // kill the Stop button and hide the running indicator mid-run.
            if (!handedOffToResume) {
                abortRef.current = null;
                setExecuting(false);
            }
            if (shouldReloadThreads) {
                // Refresh sidebar list only — do NOT reload history for the current
                // thread, as in-memory messages are already correct from SSE events.
                // Reloading would replace messages with DB state, which may lag behind.
                // Small delay lets the backend finish its async history save first.
                setTimeout(() => refreshThreadsList(workflowId), 800);
            }
        }
    };

    const stopGeneration = () => {
        const currentThread = threadId;                // capture BEFORE reset
        abortRef.current?.abort();
        setStreamingContent('');
        setStreamingAgent('');
        setFallbackStatus(null);
        setExecuting(false);
        clearAllActiveNodes();
        // Preserve the Debug Log for this interrupted run (mark it 'stopped')
        // instead of wiping it — the user asked to be able to go back and
        // review a stopped run, and re-running afterwards archives it into
        // runHistory via beginRunContext.
        stopRunPreservingLog();
        // Retry the /chat-pending fetch across five attempts so we don't
        // race the backend's post-abort snapshot write. If the last attempt
        // still returns null the banner just doesn't appear — the DB row
        // will still be there for the next thread open.
        hydrateFailureSnapshotWithRetry(currentThread);
    };

    // Abort any in-flight stream when the panel unmounts so state setters
    // don't fire on an unmounted component.
    useEffect(() => () => {
        abortRef.current?.abort();
    }, []);

    const handleHitlSubmit = async (decision, reqOverride = null, extras = null) => {
        const req = reqOverride || hitlRequest;
        if (!req) return;
        setHitlRequest(null);
        setHitlRedirectText('');
        setExecuting(true);
        setStreamingContent('');
        setStreamingAgent('');
        setFallbackStatus(null);

        // Track run-level metadata for the assistant message action bar.
        const runStartTime = Date.now();
        let runModel = '';

        const controller = new AbortController();
        abortRef.current = controller;

        // Declared outside the try so the `finally` below can read it — a `let`
        // inside the try block is not in scope there.
        let handedOffToResume = false;

        try {
            const workflow = getWorkflowForExecution();
            const resumeBody = {
                workflow,
                human_input: decision,
                workflow_id: workflowId,
                workflow_name: workflowName,
                thread_id: req.threadId,
                // Forward the run-settings choice so the resumed flow
                // honours the same swarm policy as the initial run.
                subagents_enabled: runSubagentsEnabled,
            };
            // before_tool path may carry a user-edited tool-call list. The
            // engine prefers this list over the snapshot's pending calls
            // when present; missing/null falls back to the original.
            if (extras && Array.isArray(extras.pendingToolCallsOverride)) {
                resumeBody.pending_tool_calls_override = extras.pendingToolCallsOverride;
            }
            const response = await fetch(`${API_BASE}/resume-stream`, {
                method: 'POST',
                credentials: 'include',
                headers: buildAuthHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify(resumeBody),
                signal: controller.signal,
            });

            if (!response.ok) {
                // Parse the JSON error body so structured {detail:{code,message}}
                // (e.g. budget errors) surfaces a clean message instead of a raw
                // JSON blob. Fall back to a status line for non-JSON bodies.
                let body = null;
                try { body = await response.json(); } catch { /* non-JSON error body */ }
                const msg = body
                    ? errText(body.detail, body.message, `Server error (${response.status})`)
                    : `Server error (${response.status})`;
                throw new Error(msg);
            }

            const resumeContentType = response.headers.get('Content-Type') || '';
            // Validate content-type and body type before consuming the stream.
            // This ensures response.body is only accessed after confirming it is
            // a trusted SSE stream from our own API endpoint.
            if (!resumeContentType.includes('text/event-stream')) {
                throw new Error('Unexpected response type from server');
            }
            // Real validation gate (not a rename) — rejects any object that
            // isn't an actual ReadableStream before a single byte is read
            // from it (CWE-79 hardening; see getValidatedStreamBody above).
            const validatedBody = getValidatedStreamBody(response);
            if (!validatedBody) throw new Error('No response body');
            const reader = validatedBody.getReader();
            const decoder = new TextDecoder();
            let currentAgentResponse = '';
            let sseBuffer = '';
            let hitlInterrupted = false;
            // NOTE: `handedOffToResume` is declared above the try block, since
            // the `finally` needs to read it. Set when this stream chains into a
            // further /resume-stream call (session auto-approve); that call then
            // owns abortRef and the executing flag. Mirrors the live handler.
            // Mirror of the live-stream handler's per-run accumulators so
            // resumed runs (after HITL) also surface per-node tokens and
            // the workflow-total row in the Debug Log.
            // See live-stream handler above for the char-based estimator
            // rationale — this block mirrors that logic.
            const nodeRunStats = {};
            const estimateTokens = (chars) => Math.max(0, Math.round((chars || 0) / 4));
            const recomputeStats = (stats) => {
                const inputChars  = (stats.input  || '').length;
                const outputChars = (stats.output || '').length;
                return {
                    inputChars,
                    outputChars,
                    tokensEstimate: estimateTokens(inputChars + outputChars),
                };
            };
            const trackNodeStat = (nodeId, patch) => {
                if (!nodeId) return;
                const prev = nodeRunStats[nodeId] || { chunksStreamed: 0 };
                const merged = { ...prev, ...patch };
                nodeRunStats[nodeId] = { ...merged, ...recomputeStats(merged) };
            };
            let pipedInput = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) {
                    sseBuffer += decoder.decode();
                } else {
                    sseBuffer += decoder.decode(value, { stream: true });
                }

                const rawEvents = sseBuffer.split('\n\n');
                sseBuffer = rawEvents.pop() || '';
                if (done && sseBuffer.trim()) {
                    rawEvents.push(sseBuffer);
                    sseBuffer = '';
                }

                for (const rawEvent of rawEvents) {
                    const normalized = rawEvent.replace(/\r/g, '');
                    const dataLine = normalized.split('\n').find(l => l.startsWith('data: '));
                    if (!dataLine) continue;
                    try {
                        const data = JSON.parse(dataLine.slice(6));
                        if (data.event === 'agent_start') {
                            currentAgentResponse = '';
                            setStreamingAgent(data.data.agent);
                            setStreamingContent('');
                            const activeNode = findNodeForExecutionEvent(data.data);
                            const startNodeId = data.data?.node_id || activeNode?.id || null;
                            addExecutionLog({
                                type: 'agent_start',
                                agent: data.data.agent,
                                nodeId: startNodeId,
                            });
                            if (activeNode) setNodeActive(activeNode.id);
                            trackNodeStat(startNodeId, {
                                agent: data.data?.agent,
                                input: pipedInput,
                                output: '',
                                chunksStreamed: 0,
                            });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: startNodeId,
                                title: 'Node initiated',
                                status: 'running',
                                raw: data,
                            });
                        } else if (data.event === 'agent_progress') {
                            // See live-stream handler above for rationale.
                            const progressAgent = data.data.agent;
                            if (data.data.status === 'running') {
                                setStreamingAgent(progressAgent);
                                const node = findNodeForExecutionEvent(data.data);
                                addExecutionLog({
                                    type: 'agent_start',
                                    agent: progressAgent,
                                    nodeId: data.data?.node_id || node?.id || null,
                                });
                                if (node) setNodeActive(node.id);
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    nodeId: data.data?.node_id || node?.id || null,
                                    title: 'Node initiated',
                                    status: 'running',
                                    raw: data,
                                });
                            } else if (data.data.status === 'done') {
                                addExecutionLog({
                                    type: 'agent_complete',
                                    agent: progressAgent,
                                    nodeId: data.data?.node_id || null,
                                });
                                // Node finished — drop its transient retry/fallback
                                // status so it doesn't bleed onto the next node.
                                setFallbackStatus(null);
                                const node = findNodeForExecutionEvent(data.data);
                                if (node) clearNodeActive(node.id);
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    nodeId: data.data?.node_id || node?.id || null,
                                    title: 'Node processing is completed',
                                    status: 'done',
                                    raw: data,
                                });
                            }
                        } else if (data.event === 'agent_token') {
                            // Strip control characters from untrusted streamed
                            // model/tool output before it enters app state and
                            // is rendered as markdown (CWE-79 / Reflected XSS
                            // hardening; see sanitizeStreamToken above).
                            currentAgentResponse += sanitizeStreamToken(data.data.token);
                            setStreamingContent(currentAgentResponse);
                            // Chunk count is a secondary signal only —
                            // char-based token estimate happens at
                            // agent_complete. See live-stream handler.
                            const tokNodeId = data.data?.node_id || null;
                            if (tokNodeId) {
                                const prev = nodeRunStats[tokNodeId] || { chunksStreamed: 0 };
                                nodeRunStats[tokNodeId] = {
                                    ...prev,
                                    chunksStreamed: (prev.chunksStreamed || 0) + 1,
                                };
                            }
                        } else if (data.event === 'agent_retry') {
                            handleRetryNotice(data, { setFallbackStatus, pushDebugRow });
                        } else if (data.event === 'agent_fallback') {
                            handleFallbackNotice(data, { setFallbackStatus, pushDebugRow });
                        } else if (data.event === 'agent_complete') {
                            const completedNode = findNodeForExecutionEvent(data.data);
                            if (completedNode) clearNodeActive(completedNode.id);
                            // Node finished — drop its transient retry/fallback
                            // status so it doesn't bleed onto the next node.
                            setFallbackStatus(null);
                            // Track the model used by the terminal agent for
                            // the message action bar metadata display.
                            if (data.data?.model) runModel = data.data.model;
                            const completedNodeId = data.data?.node_id || completedNode?.id || null;
                            addExecutionLog({
                                type: 'agent_complete',
                                agent: data.data.agent,
                                nodeId: completedNodeId,
                                output: data.data.output,
                                generatedFiles: data.data.generated_files || [],
                            });
                            const out = typeof data.data?.output === 'string' ? data.data.output : '';
                            trackNodeStat(completedNodeId, { agent: data.data?.agent, output: out, input: pipedInput });
                            if (out) pipedInput = out;
                            const stats = nodeRunStats[completedNodeId] || {};
                            const enrichedRaw = {
                                event: 'agent_complete',
                                node_id: completedNodeId,
                                agent: data.data?.agent,
                                input: stats.input || '',
                                output: out,
                                input_chars: stats.inputChars || 0,
                                output_chars: stats.outputChars || 0,
                                tokens_estimate: stats.tokensEstimate || 0,
                                tokens_estimate_note:
                                    'Char-based estimate (chars/4). Under-counts real usage — '
                                    + 'ignores system prompt + tool definitions + intermediate '
                                    + 'tool-calling turns.',
                                chunks_streamed: stats.chunksStreamed || 0,
                                usage: data.data?.usage || null,
                                model: data.data?.model || '',
                                generated_files: data.data?.generated_files || [],
                                engine_event: data,
                            };
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: completedNodeId,
                                title: 'Agent execution successful',
                                detail: usageSummaryText(data.data?.usage) || ((stats.tokensEstimate || 0) > 0
                                    ? `~${stats.tokensEstimate.toLocaleString()} tokens · `
                                      + `${(stats.inputChars || 0).toLocaleString()} chars in / `
                                      + `${(stats.outputChars || 0).toLocaleString()} chars out`
                                    : ''),
                                status: 'done',
                                generatedFiles: data.data?.generated_files || null,
                                raw: enrichedRaw,
                            });
                        } else if (data.event === 'complete') {
                            setStreamingContent('');
                            setStreamingAgent('');
                            setFallbackStatus(null);
                            // See live-stream `complete` handler for rationale —
                            // capture prior status BEFORE the complete payload
                            // overwrites it so we know whether to render a
                            // success row or an "error finished" row.
                            const priorStatus = useWorkflowStore.getState().runContext.status;
                            const rejected = !!data.data.hitl_rejected;
                            setRunContextFromComplete(data.data || {});
                            const finalOutputText = safeString(data.data?.output) || '';
                            // Story-style bookend rows — every run ends
                            // with an explicit "End" row, then an "Output"
                            // row showing what the assistant produced. On
                            // an errored / rejected run we skip the Output
                            // row because the engine echoes the error
                            // string into `data.output` and we don't want
                            // to mislead the user by labelling that as
                            // "Output". The Error row above already
                            // surfaces the failure.
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                kind: 'End',
                                nodeLabel: 'End',
                                title: priorStatus === 'error'
                                    ? 'Workflow ended with errors'
                                    : (rejected ? 'Workflow stopped by reviewer' : 'Workflow execution finished'),
                                status: priorStatus === 'error' || rejected ? 'error' : 'done',
                                raw: data,
                            });
                            if (priorStatus !== 'error' && !rejected) {
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    kind: 'Output',
                                    nodeLabel: 'Output',
                                    title: finalOutputText || '(no output)',
                                    status: 'done',
                                    raw: { event: 'final_output', data: { text: finalOutputText } },
                                });
                            }
                            {
                                const usage = data.data?.usage || null;
                                const perNode = nodeRunStats || {};
                                const totals = Object.values(perNode).reduce(
                                    (acc, s) => ({
                                        inputChars:  acc.inputChars  + (s.inputChars  || 0),
                                        outputChars: acc.outputChars + (s.outputChars || 0),
                                        chunksStreamed: acc.chunksStreamed + (s.chunksStreamed || 0),
                                    }),
                                    { inputChars: 0, outputChars: 0, chunksStreamed: 0 },
                                );
                                const totalChars = totals.inputChars + totals.outputChars;
                                const totalTokensEstimate = estimateTokens(totalChars);
                                const nodeCount = Object.keys(perNode).length;
                                const hasBackendUsage = !!usage && (
                                    Number(usage.total_tokens || 0) > 0
                                    || Number(usage.prompt_tokens || 0) > 0
                                    || Number(usage.completion_tokens || 0) > 0
                                    || Number(usage.cost_usd || 0) > 0
                                );
                                pushDebugRow({
                                    ts: new Date().toISOString(),
                                    kind: 'Tokens',
                                    nodeLabel: hasBackendUsage ? 'Usage' : 'Tokens (approx)',
                                    title: hasBackendUsage
                                        ? `${formatUsageValue(usage.total_tokens || ((usage.prompt_tokens || 0) + (usage.completion_tokens || 0)))} tokens${usage.estimated ? ' (estimated)' : ''} · ${formatCostUsd(usage.cost_usd)}`
                                        : `~${totalTokensEstimate.toLocaleString()} tokens (est. from ${totalChars.toLocaleString()} chars in/out across ${nodeCount} node${nodeCount === 1 ? '' : 's'})`,
                                    detail: hasBackendUsage
                                        ? `${formatUsageValue(usage.prompt_tokens)} prompt · ${formatUsageValue(usage.completion_tokens)} completion`
                                        : `${totals.inputChars.toLocaleString()} chars in · ${totals.outputChars.toLocaleString()} chars out · ${totals.chunksStreamed.toLocaleString()} SSE chunks streamed`,
                                    status: 'done',
                                    raw: {
                                        event: 'token_usage_summary',
                                        usage,
                                        total_tokens_estimate: totalTokensEstimate,
                                        total_input_chars:  totals.inputChars,
                                        total_output_chars: totals.outputChars,
                                        total_chars:        totalChars,
                                        total_chunks_streamed: totals.chunksStreamed,
                                        node_count: nodeCount,
                                        per_node: perNode,
                                    },
                                });
                            }
                            const output = safeString(data.data.output) || 'No output received';
                            setExecutionResult(output);
                            const allFiles = (data.data.generated_files || []);
                            const runDurationS = runStartTime
                                ? Math.round((Date.now() - runStartTime) / 1000)
                                : null;
                            setMessages(prev => [...prev, {
                                type: rejected ? 'hitl-rejected' : 'assistant',
                                content: rejected
                                    ? 'Run stopped — reviewer rejected the output. Adjust the agent\u2019s instructions or input and try again.'
                                    : output,
                                trace: data.data.execution_trace,
                                generatedFiles: allFiles,
                                usage: data.data?.usage || null,
                                model: runModel || '',
                                durationS: runDurationS,
                            }]);
                            setExecuting(false);
                        } else if (data.event === 'error') {
                            setStreamingContent('');
                            setStreamingAgent('');
                            setFallbackStatus(null);
                            // Backend budget errors send data.detail as a
                            // {code, message} object — unwrap to a clean string
                            // so the error card doesn't show "[object Object]".
                            const errMsg = errText(data.data.detail, data.data.message, 'Unknown error occurred');
                            setExecutionError(errMsg);
                            setMessages(prev => [...prev, { type: 'error', content: errMsg }]);
                            setRunStatus('error');
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                title: 'Error',
                                detail: errMsg,
                                status: 'error',
                                raw: data,
                            });
                            // Fallback trigger for the retry banner on the
                            // resume path (see /run-stream branch for the
                            // rationale — /chat-pending poll can be racy).
                            if (data.data?.retryable === true && data.data?.node_id) {
                                setFailureSnapshot({
                                    threadId: threadId,
                                    nodeId: data.data.node_id,
                                    agent: data.data.agent || '',
                                    error: data.data.message || 'Node failed',
                                    errorType: '',
                                    completedNodes: [],
                                    lastInput: '',
                                    documents: [],
                                });
                            }
                            // Also poll with retries so the banner appears
                            // even when the error event omits retryable —
                            // the snapshot on disk is the source of truth.
                            hydrateFailureSnapshotWithRetry(threadId);
                        } else if (data.event === 'tool_call_start') {
                            addExecutionLog({ type: 'tool_call', agent: data.data.agent, tool: data.data.tool_name, args: data.data.arguments });
                            // Same delegation special-case as the streaming-agent
                            // branch above — surface a professional sentence rather
                            // than the raw delegate_to_<alias> identifier.
                            if (typeof data.data.tool_name === 'string'
                                && data.data.tool_name.startsWith('delegate_to_')) {
                                const alias = data.data.tool_name.slice('delegate_to_'.length);
                                setStreamingAgent(`Delegating this task to the ${alias} subagent…`);
                            } else {
                                setStreamingAgent(`${data.data.agent} -> ${data.data.tool_name}`);
                            }
                        } else if (data.event === 'tool_call_result') {
                            addExecutionLog({ type: 'tool_result', agent: data.data.agent, tool: data.data.tool_name, result: data.data.result });
                            setStreamingAgent(`${data.data.agent} working...`);
                        } else if (data.event === 'swarm_plan') {
                            addExecutionLog({
                                type:    'swarm_plan',
                                runId:   data.data?.run_id,
                                nodeId:  data.data?.node_id || null,
                                strategy: data.data?.strategy,
                                roleIds: data.data?.role_ids || [],
                                workerCount: data.data?.worker_count || 0,
                            });
                            const n = data.data?.worker_count || 0;
                            if (n > 0) {
                                setStreamingAgent(`Planning ${n} sub-agent${n === 1 ? '' : 's'}…`);
                            }
                        } else if (data.event === 'swarm_error') {
                            addExecutionLog({
                                type:   'swarm_error',
                                runId:  data.data?.run_id,
                                nodeId: data.data?.node_id || null,
                                code:   data.data?.code || 'swarm_error',
                                detail: data.data?.detail || '',
                            });
                        } else if (data.event === 'kb_retrieval') {
                            // Resume-stream parity with the main run path — see
                            // the identical handler above for the field contract.
                            const kbChunks = Array.isArray(data.data?.chunks) ? data.data.chunks : [];
                            const kbCount = data.data?.chunk_count ?? kbChunks.length;
                            const kbConf = data.data?.confidence;
                            const confStr = (kbConf === null || kbConf === undefined)
                                ? '' : ` · confidence ${Number(kbConf).toFixed(2)}`;
                            addExecutionLog({
                                type:       'kb_retrieval',
                                nodeId:     data.data?.node_id || null,
                                agent:      data.data?.agent,
                                mode:       data.data?.mode,
                                query:      data.data?.query,
                                chunkCount: kbCount,
                                confidence: kbConf ?? null,
                                chunks:     kbChunks,
                            });
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: data.data?.node_id || null,
                                kind: 'Knowledge',
                                nodeLabel: `Knowledge retrieval${data.data?.agent ? ` · ${data.data.agent}` : ''}`,
                                title: kbCount > 0
                                    ? `${kbCount} chunk${kbCount === 1 ? '' : 's'} qualified${confStr}`
                                    : `No chunks matched${confStr}`,
                                detail: data.data?.query ? `query: ${data.data.query}` : '',
                                status: 'done',
                                raw: {
                                    event: 'kb_retrieval',
                                    node_id: data.data?.node_id,
                                    agent: data.data?.agent,
                                    mode: data.data?.mode,
                                    query: data.data?.query,
                                    chunk_count: kbCount,
                                    confidence: kbConf ?? null,
                                    chunks: kbChunks,
                                    engine_event: data,
                                },
                            });
                        } else if (data.event === 'subagent_start') {
                            addExecutionLog({
                                type: 'subagent_start',
                                callId:        data.data.call_id,
                                nodeId:        data.data.node_id || null,
                                alias:         data.data.alias,
                                agentId:       data.data.agent_id,
                                parentAgentId: data.data.parent_agent_id,
                                taskPreview:   data.data.task_preview,
                                tools:         Array.isArray(data.data.tools)  ? data.data.tools  : [],
                                skills:        Array.isArray(data.data.skills) ? data.data.skills : [],
                            });
                            setStreamingAgent(`Delegating this task to the ${data.data.alias} subagent…`);
                        } else if (data.event === 'subagent_complete') {
                            addExecutionLog({
                                type: 'subagent_complete',
                                callId:        data.data.call_id,
                                nodeId:        data.data.node_id || null,
                                alias:         data.data.alias,
                                agentId:       data.data.agent_id,
                                parentAgentId: data.data.parent_agent_id,
                                durationS:     data.data.duration_s,
                                ok:            data.data.ok,
                                error:         data.data.error,
                                files:         data.data.files || [],
                                preview:       data.data.preview || '',
                            });
                        } else if (data.event === 'condition_flash') {
                            setNodeActive(data.data.node_id);
                            setTimeout(() => clearNodeActive(data.data.node_id), 400);
                        } else if (data.event === 'loop_iteration_start') {
                            const { node_id, mode, index, total } = data.data || {};
                            if (node_id) {
                                setLoopProgress(node_id, { running: true, index, total, mode });
                                addExecutionLog({ type: 'loop_iter', nodeId: node_id, index, total, mode });
                            }
                        } else if (data.event === 'loop_complete') {
                            const { node_id, total_iterations, max_iterations_hit } = data.data || {};
                            if (node_id) {
                                clearLoopProgress(node_id);
                                addExecutionLog({ type: 'loop_done', nodeId: node_id, total: total_iterations, maxHit: !!max_iterations_hit });
                            }
                        } else if (data.event === 'loop_condition_eval') {
                            const { node_id, index, will_continue, case_results, eval_state, evaluator_pending } = data.data || {};
                            if (node_id) {
                                addExecutionLog({
                                    type: 'loop_condition',
                                    nodeId: node_id,
                                    index,
                                    willContinue: !!will_continue,
                                    caseResults: case_results || [],
                                    evalState: eval_state || null,
                                    evaluatorPending: !!evaluator_pending,
                                });
                            }
                        } else if (handleLoopSummaryEvent(data.event, data.data, { addExecutionLog, setMessages })) {
                            // Handled by the shared loop-summary dispatcher.
                        } else if (data.event === 'hitl_interrupt') {
                            // Resume produced ANOTHER pause (e.g. multi-step
                            // before_tool). Re-render the card.
                            const payload = data.data || {};
                            const reason = payload.reason || 'after_response';
                            let interruptType = reason;
                            let question = '';
                            let options = [];
                            let prompt = '';
                            if (reason === 'ask_human') {
                                question = (payload.payload && payload.payload.question) || '';
                                options = ((payload.payload && payload.payload.options) || []);
                                prompt = question;
                            } else if (reason === 'before_tool') {
                                const calls = payload.pending_tool_calls || [];
                                const first = calls[0] || {};
                                prompt = buildToolSummary(first.name, first.args || {});
                            } else {
                                prompt = payload.output || '';
                            }
                            // Session auto-approve short-circuit — MUST be
                            // mirrored here, not only in the live-stream
                            // handler.
                            //
                            // The backend re-arms the before_tool gate after
                            // every approved tool call, and each pause ends its
                            // stream. So the FIRST pause of a run arrives on
                            // /run-stream, but the second and every subsequent
                            // one arrive here, on /resume-stream. Checking the
                            // flag only in the live-stream handler meant
                            // "Allow all this session" suppressed exactly one
                            // card and then prompted again for the very same
                            // skill on the next round.
                            if (interruptType === 'before_tool' && sessionAutoApproveRef.current) {
                                handedOffToResume = true;
                                hitlInterrupted = true;
                                handleHitlSubmit('APPROVED', {
                                    threadId: payload.thread_id,
                                    interruptType, agent: payload.agent,
                                    prompt, question, options,
                                });
                            } else {
                                // Clear partial streaming before swapping in the
                                // HITL card (same rationale as the live-stream
                                // handler).
                                setStreamingContent('');
                                setStreamingAgent('');
                                setFallbackStatus(null);
                                setHitlRequest({
                                    threadId: payload.thread_id,
                                    interruptType,
                                    agent: payload.agent || 'Agent',
                                    nodeId: payload.node_id || '',
                                    prompt, question, options,
                                    pendingToolCalls: payload.pending_tool_calls || [],
                                });
                                setHitlRedirectText('');
                                setExecuting(false);
                                hitlInterrupted = true;
                            }
                        } else if (data.event === 'workflow_retrying') {
                            // Engine restarted the failed node on resume.
                            // Informational timeline row, mirrors the
                            // /run-stream branch above.
                            pushDebugRow({
                                ts: new Date().toISOString(),
                                nodeId: data.data?.node_id || null,
                                title: `Retrying node`,
                                detail: `Retrying ${data.data?.agent || 'node'} after previous failure: ${data.data?.previous_error || ''}`.slice(0, 240),
                                status: 'info',
                                raw: data,
                            });
                        }
                    } catch (e) { /* Skip invalid JSON */ }
                    if (hitlInterrupted) break;
                }

                if (done || hitlInterrupted) break;
            }
            // Release the reader lock — a resumed run can pause again, and we
            // leave this loop by `break` rather than draining to `done`.
            if (hitlInterrupted) {
                try { await reader.cancel(); } catch { /* already closed */ }
            }
        } catch (error) {
            setStreamingContent('');
            setStreamingAgent('');
            setFallbackStatus(null);
            if (error.name !== 'AbortError') {
                setExecutionError(error.message);
                setMessages(prev => [...prev, { type: 'error', content: `Error: ${error.message}` }]);
            }
        } finally {
            // A chained auto-approve resume now owns abortRef and the
            // executing flag — clearing them here would break the Stop button
            // and hide the running indicator mid-run.
            if (!handedOffToResume) {
                abortRef.current = null;
                setExecuting(false);
            }
        }
    };

    // Approve / Save & approve / Allow-all entry point for the before_tool
    // card. Carries the user-edited list (`editedToolCalls`) to the engine
    // via pending_tool_calls_override.
    //
    // When `persist` is true we also write the edits onto the agent node's
    // data.tools array and PUT the workflow so the change survives reload.
    // Persist is intentionally NEVER set by Allow-all-this-session — that
    // flag is for the live tab only and must not mutate the saved graph.
    const submitWithToolEdits = async ({ persist = false } = {}) => {
        const req = hitlRequest;
        if (!req) return;
        const overrideList = editedToolCalls.map((tc) => ({
            id:   tc.id || '',
            name: tc.name || '',
            args: tc.args && typeof tc.args === 'object' ? tc.args : {},
        }));

        if (persist) {
            // Locate the agent node that owns this interrupt. Prefer the
            // explicit node_id from the SSE payload (already cached on the
            // hitlRequest), then fall back to a name match for older
            // snapshots that don't carry node_id.
            const node = (req.nodeId && nodes.find((n) => n.id === req.nodeId))
                || nodes.find((n) => n.type === 'agent' && n.data?.name === req.agent);
            if (node && workflowId) {
                const existingTools = Array.isArray(node.data?.tools) ? node.data.tools : [];
                const keptNames = new Set(overrideList.map((tc) => tc.name));
                const originalNames = new Set(
                    (req.pendingToolCalls || []).map((tc) => tc?.name).filter(Boolean),
                );
                // Drop tools the user removed from this turn's pending list,
                // BUT only if they were originally pending. Tools the agent
                // has attached for other purposes stay put.
                const filtered = existingTools.filter(
                    (t) => !originalNames.has(t.name) || keptNames.has(t.name),
                );
                // Add any tools the user introduced via "use X" that aren't
                // already attached. Catalog lookup happens server-side, so we
                // only need the name here.
                const haveNames = new Set(filtered.map((t) => t.name));
                const additions = overrideList
                    .filter((tc) => tc.name && !haveNames.has(tc.name))
                    .map((tc) => ({ name: tc.name }));
                const nextTools = [...filtered, ...additions];

                updateNodeData(node.id, { tools: nextTools });

                // Build the next graphData using the freshly-updated node
                // list. We can't read from the store synchronously because
                // updateNodeData is a set() — replicate the change locally.
                const nextNodes = nodes.map((n) =>
                    n.id === node.id
                        ? { ...n, data: { ...(n.data || {}), tools: nextTools } }
                        : n,
                );
                const edges = useWorkflowStore.getState().edges;
                try {
                    await updateWorkflowRecord(workflowId, {
                        name: workflowName,
                        graphData: { nodes: nextNodes, edges },
                    });
                } catch (e) {
                    // Persistence failed (e.g. 409 stale). Still proceed with
                    // the per-turn override so the user's run isn't blocked,
                    // and surface the failure in the chat log.
                    console.error('Save & approve: workflow persist failed', e);
                }
            }
        }

        await handleHitlSubmit('approve', null, { pendingToolCallsOverride: overrideList });
    };

    const handleKeyPress = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    };


    return (
        <div className="chat-panel animate-slide-in-right" style={style}>
            {isHistoryOpen && (
                <div className="chat-history-overlay">
                    <div className="chat-history-panel" ref={historyPanelRef}>
                        <div className="chat-sidebar-header">
                            <div>
                                <span className="chat-sidebar-kicker">Conversations</span>
                                <strong>History</strong>
                            </div>
                        </div>

                        <div className="chat-history-search">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                <circle cx="11" cy="11" r="8"></circle>
                                <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                            </svg>
                            <input
                                type="text"
                                placeholder="Search conversations"
                                value={historySearch}
                                onChange={(e) => setHistorySearch(e.target.value)}
                                autoFocus
                            />
                            {historySearch && (
                                <button
                                    className="chat-history-search-clear"
                                    onClick={() => setHistorySearch('')}
                                    title="Clear search"
                                >
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                        <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                                    </svg>
                                </button>
                            )}
                        </div>

                        <div className="chat-history-list">
                            {groupedThreads.length === 0 ? (
                                <div className="chat-history-empty">
                                    {threads.length === 0 ? 'No previous chats yet.' : 'No chat matches your search.'}
                                </div>
                            ) : groupedThreads.map(([group, items]) => (
                                <div className="chat-history-group" key={group}>
                                    <div className="chat-history-group-title">{group}</div>
                                    {items.map((thread) => (
                                        <div
                                            key={thread.thread_id}
                                            className={`chat-history-item${thread.thread_id === threadId ? ' active' : ''}`}
                                            onClick={() => handleThreadSelect(thread.thread_id)}
                                        >
                                            <span className="chat-history-item-main">
                                                <span className="chat-history-item-title" title={threadTitle(thread)}>
                                                    {thread.has_pending_interrupt && (
                                                        <span
                                                            className="chat-history-item-paused"
                                                            title="Paused — waiting for human input"
                                                            style={{
                                                                display: 'inline-block',
                                                                width: 6, height: 6,
                                                                borderRadius: '50%',
                                                                background: '#f59e0b',
                                                                marginRight: 6,
                                                                verticalAlign: 'middle',
                                                                boxShadow: '0 0 0 2px rgba(245, 158, 11, 0.25)',
                                                            }}
                                                        />
                                                    )}
                                                    {threadTitle(thread)}
                                                </span>
                                                <span className="chat-history-item-preview">{threadPreview(thread)}</span>
                                            </span>
                                            <span className="chat-history-item-meta">
                                                <span className="chat-history-item-time">{formatRelativeTime(thread.last_updated)}</span>
                                                <button
                                                    className="chat-history-item-delete"
                                                    onClick={(e) => handleDeleteThread(e, thread.thread_id)}
                                                    title="Delete conversation"
                                                >
                                                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                        <polyline points="3 6 5 6 21 6" />
                                                        <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                                                        <path d="M10 11v6M14 11v6" />
                                                        <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
                                                    </svg>
                                                </button>
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            )}

            <section className="chat-main">
                <div className="chat-header chat-header--preview">
                    <div className="chat-header-left">
                        <div className="chat-title-stack">
                            <span className="chat-eyebrow">Preview</span>
                            <span className="chat-title">{workflowName || 'Workflow'}</span>
                        </div>
                    </div>
                    {/* Header action order (left → right): Settings, Debug,
                        History, New chat. Grouped by frequency — rarely-touched
                        config on the left, the primary "New chat" action
                        anchored on the far right. */}
                    <div className="chat-header-actions">
                        {/* Run-settings popover trigger. Owns its own open/close
                            state; writes through to
                            useWorkflowStore.runSubagentsEnabled, read below when
                            assembling the /run-stream payload. */}
                        <RunSettingsStrip />
                        {/* Debug Log toggle. Click swaps the chat body for
                            <DebugLogView/> (full swap, not overlay) and shows the
                            .active style so the user can tell which mode they're
                            in. */}
                        <button
                            type="button"
                            className={`chat-icon-btn${isDebugOpen ? ' active' : ''}`}
                            onClick={() => setIsDebugOpen((prev) => !prev)}
                            title={isDebugOpen ? 'Close Debug Log' : 'Open Debug Log'}
                            aria-label="Toggle Debug Log"
                            aria-pressed={isDebugOpen}
                        >
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <rect x="8" y="6" width="8" height="14" rx="4" />
                                <path d="M12 6V3M5 9l3 1M19 9l-3 1M5 15l3-1M19 15l-3-1M9 20l-2 2M15 20l2 2" />
                            </svg>
                        </button>
                        <button
                            ref={historyButtonRef}
                            className={`chat-icon-btn${isHistoryOpen ? ' active' : ''}`}
                            onClick={() => {
                                setIsHistoryOpen((prev) => !prev);
                                setHistorySearch('');
                            }}
                            disabled={isExecuting || isLoadingHistory}
                            title="History"
                            aria-label="Chat history"
                        >
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M3 12a9 9 0 1 0 3-6.7"></path>
                                <polyline points="3 3 3 9 9 9"></polyline>
                                <path d="M12 7v6l4 2"></path>
                            </svg>
                        </button>
                        <button
                            className="chat-icon-btn"
                            onClick={handleNewChat}
                            disabled={isExecuting || isLoadingHistory}
                            title="New chat"
                            aria-label="Start new chat"
                        >
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M12 5v14M5 12h14"></path>
                            </svg>
                        </button>
                    </div>
                </div>

            {/* Debug Log full-swap. When toggled on, the chat body
                (messages + composer) is hidden via CSS (`.chat-body-hidden`)
                so all chat state — streaming text, attachments, hitl
                widgets — survives the swap unchanged, and we just overlay
                the debug view in its place inside the same .chat-main. */}
            {isDebugOpen ? (
                <DebugLogView
                    runContext={runContext}
                    onMinimize={() => setIsDebugOpen(false)}
                    onClose={() => setIsDebugOpen(false)}
                />
            ) : null}

            <div
                className={`chat-body${isDebugOpen ? ' chat-body-hidden' : ''}`}
                aria-hidden={isDebugOpen ? 'true' : 'false'}
            >
            <DownloadNotice notice={downloadNotice} />

            <div className="chat-messages">
                {isLoadingHistory ? (
                    <div className="chat-empty chat-empty-loading">
                        <div className="assistant-row">
                            <div className="assistant-avatar">AI</div>
                            <div className="assistant-message">
                                <div className="typing-indicator"><span></span><span></span><span></span></div>
                            </div>
                        </div>
                    </div>
                ) : messages.length === 0 ? (
                    <div className="chat-empty chat-empty--preview">
                        {backendStatus === null ? (
                            <div className="chat-empty-status">
                                <span className="chat-empty-spinner" aria-hidden="true" />
                                <p>Connecting…</p>
                            </div>
                        ) : backendStatus === 'error' ? (
                            <div className="chat-empty-status chat-empty-status--error">
                                <p>Backend offline. Retrying automatically.</p>
                            </div>
                        ) : (
                            <div className="chat-empty-card">
                                <div className="chat-empty-icon-wrap" aria-hidden="true">
                                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                                        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                                    </svg>
                                </div>
                                <h3 className="chat-empty-title">Try your workflow</h3>
                                <p className="chat-empty-sub">
                                    Send a message below to see how
                                    {' '}<strong>{workflowName || 'this workflow'}</strong>{' '}
                                    responds in real time.
                                </p>
                            </div>
                        )}
                    </div>
                ) : (
                    <>
                        {messages.map((msg, idx) => (
                            <div key={msg.id ?? idx} className={`chat-message ${msg.type}`}>
                                {msg.type === 'user' && (safeString(msg.content).trim() || (Array.isArray(msg.attachments) && msg.attachments.length > 0)) && (
                                    <div className="message-stack">
                                        <div className="message-bubble user-bubble">
                                            {safeString(msg.content).trim() && (
                                                <div className="user-bubble-text">
                                                    {safeString(msg.content)}
                                                </div>
                                            )}
                                            {Array.isArray(msg.attachments) && msg.attachments.length > 0 && (
                                                <div className="user-bubble-attachments">
                                                    {msg.attachments.map((att, i) => (
                                                        <div
                                                            key={`${att.file_name}-${i}`}
                                                            className="user-bubble-attachment"
                                                            title={att.file_size ? `${att.file_name} • ${_formatFileSize(att.file_size)}` : att.file_name}
                                                        >
                                                            <span className="user-bubble-attachment__icon" aria-hidden="true">
                                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                                                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                                                                    <polyline points="14 2 14 8 20 8" />
                                                                </svg>
                                                            </span>
                                                            <span className="user-bubble-attachment__meta">
                                                                <span className="user-bubble-attachment__name">{att.file_name}</span>
                                                                {att.file_size ? (
                                                                    <span className="user-bubble-attachment__size">{_formatFileSize(att.file_size)}</span>
                                                                ) : null}
                                                            </span>
                                                        </div>
                                                    ))}
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                )}
                                {msg.type === 'assistant' && (() => {
                                    const isLatestAssistant = idx === messages.length - 1;
                                    // For the latest message, prefer streamingContent (live tokens)
                                    // over msg.content which may be stale or empty mid-stream.
                                    const content = safeString(
                                        isLatestAssistant && streamingContent ? streamingContent : msg.content
                                    );
                                    // The factory chat endpoints don't emit a structured
                                    // `generated_files`, so fall back to sniffing the prose.
                                    const effectiveFiles = (Array.isArray(msg.generatedFiles) && msg.generatedFiles.length > 0)
                                        ? msg.generatedFiles
                                        : sniffGeneratedFiles(content, generatedFileExcludeNames);
                                    const hasFiles = effectiveFiles && effectiveFiles.length > 0;
                                    // Drop the raw path AND any markdown-link form
                                    // (`[Download X](/generated-files/X)`) from prose once a
                                    // card renders for it — otherwise the AI's inline link would
                                    // show as a plain blue text link ALONGSIDE the styled button
                                    // card, which looked inconsistent across document types.
                                    const displayContent = hasFiles
                                        ? stripGeneratedMarkdownLinks(stripBareGeneratedPaths(content))
                                        : content;
                                    const modelLabel = msg.model || '';
                                    return (
                                    <div className="assistant-row">
                                        <div className="assistant-avatar">AI</div>
                                        <div className="assistant-message markdown-content">
                                            <ReactMarkdown
                                                remarkPlugins={markdownRemarkPlugins}
                                                components={
                                                    hasFiles
                                                        ? buildMarkdownComponents(effectiveFiles, handleDownloadGenerated, generatedFileExcludeNames)
                                                        : markdownComponents
                                                }
                                            >
                                                {stripEmoji(displayContent)}
                                            </ReactMarkdown>
                                            {/* Download strip — shows only the primary deliverable.
                                                Primary = last file whose extension is a user-facing
                                                document type (pptx, docx, pdf, xlsx, csv, zip, etc.).
                                                Intermediate tool outputs (json, log, txt helper files)
                                                are hidden. Falls back to all files if no primary found. */}
                                            {hasFiles && (() => {
                                                const isPrimary = (f) => {
                                                    if (!f || !f.download_url) return false;
                                                    const ext = (f.filename || '').split('.').pop().toLowerCase();
                                                    return PRIMARY_DOWNLOAD_EXTS.has(ext);
                                                };
                                                // Collect all valid files first (excluding inline-code ones).
                                                const validFiles = effectiveFiles.filter((f) => {
                                                    if (!f || !f.download_url) return false;
                                                    const inInlineCode = f.filename && content.includes('`' + f.filename + '`');
                                                    return !inInlineCode;
                                                });
                                                // Pick the last primary file as the main deliverable.
                                                const primaryFiles = validFiles.filter(isPrimary);
                                                const stripFiles = primaryFiles.length > 0
                                                    ? [primaryFiles[primaryFiles.length - 1]]
                                                    : validFiles;
                                                if (stripFiles.length === 0) return null;
                                                return (
                                                    <div className="generated-files-strip">
                                                        {stripFiles.map((f) => (
                                                            <FileDownloadCard
                                                                key={f.download_url}
                                                                href={`${API_BASE}${f.download_url}`}
                                                                filename={f.filename || 'file'}
                                                                label={null}
                                                                busy={isFileDownloading(`${API_BASE}${f.download_url}`)}
                                                                onDownload={handleDownloadGenerated}
                                                            />
                                                        ))}
                                                    </div>
                                                );
                                            })()}
                                            {/* Action bar: copy, share, Teams share, regenerate. */}
                                            {content.trim() && (
                                                <div className="message-action-bar">
                                                    <button
                                                        type="button"
                                                        className="message-action-btn message-action-btn--copy"
                                                        title="Copy response"
                                                        onClick={() => handleCopyMessage(content, msg.id ?? idx)}
                                                    >
                                                        {copiedMsgId === (msg.id ?? idx) ? (
                                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#16a34a" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                                                <polyline points="20 6 9 17 4 12" />
                                                            </svg>
                                                        ) : (
                                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                                                <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                                                                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                                                            </svg>
                                                        )}
                                                    </button>
                                                    <button
                                                        type="button"
                                                        className="message-action-btn message-action-btn--share"
                                                        title="Share response"
                                                        onClick={() => handleShareMessage(content)}
                                                    >
                                                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                                            <circle cx="18" cy="5" r="3" />
                                                            <circle cx="6" cy="12" r="3" />
                                                            <circle cx="18" cy="19" r="3" />
                                                            <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
                                                            <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
                                                        </svg>
                                                    </button>
                                                    <button
                                                        type="button"
                                                        className="message-action-btn message-action-btn--teams"
                                                        title="Share to Teams"
                                                        onClick={() => handleTeamsShare(content)}
                                                    >
                                                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                                            <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                                                            <circle cx="9" cy="7" r="4" />
                                                            <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
                                                            <path d="M16 3.13a4 4 0 0 1 0 7.75" />
                                                        </svg>
                                                    </button>
                                                    {isLatestAssistant && !isExecuting && (
                                                        <button
                                                            type="button"
                                                            className="message-action-btn message-action-btn--regenerate"
                                                            title="Regenerate response"
                                                            onClick={handleRegenerate}
                                                        >
                                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                                                <polyline points="23 4 23 10 17 10" />
                                                                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
                                                            </svg>
                                                        </button>
                                                    )}
                                                    {msg.durationS != null && (
                                                        <span className="agent-usage-chip agent-usage-chip--dur" title="Total execution time">
                                                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                                                            {msg.durationS}s
                                                        </span>
                                                    )}
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                    );
                                })()}
                                {msg.type === 'error' && (
                                    <div className="message-bubble error-bubble chat-error-card">
                                        <strong>Something stopped the run</strong>
                                        <span>{safeString(msg.content)}</span>
                                        {lastUserMessage && (
                                            <button
                                                type="button"
                                                className="chat-retry-btn"
                                                onClick={handleRestorePrompt}
                                            >
                                                Restore prompt
                                            </button>
                                        )}
                                    </div>
                                )}
                                {/* Distinct rejection notice — different
                                    from `error` because the run was stopped
                                    intentionally by the human reviewer, not
                                    by a failure. Uses the platform's
                                    --color-error token so it matches the
                                    active theme. */}
                                {msg.type === 'hitl-rejected' && (
                                    <div className="hitl-rejected-card">
                                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                                            <circle cx="12" cy="12" r="10" />
                                            <line x1="15" y1="9" x2="9" y2="15" />
                                            <line x1="9" y1="9" x2="15" y2="15" />
                                        </svg>
                                        <div className="hitl-rejected-card-body">
                                            <strong>Rejected by reviewer</strong>
                                            <span>{safeString(msg.content)}</span>
                                            {lastUserMessage && (
                                                <button
                                                    type="button"
                                                    className="chat-retry-btn"
                                                    onClick={handleRestorePrompt}
                                                >
                                                    Restore prompt
                                                </button>
                                            )}
                                        </div>
                                    </div>
                                )}
                                {/* Session-aborted system note — rendered
                                    after the user clicks "Abort session"
                                    on the paused-run banner. Neutral
                                    styling (grey, not red) because this
                                    is a deliberate user action, not a
                                    failure. Includes the discarded
                                    agent name + a local timestamp so a
                                    scroll-back later shows a clear
                                    audit trail of when the checkpoint
                                    was thrown away. */}
                                {msg.type === 'session-aborted' && (
                                    <div className="session-aborted-card" role="status" aria-live="polite">
                                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                                            <path d="M3 6h18" />
                                            <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                                            <path d="M6 6l1 14a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-14" />
                                            <line x1="10" y1="11" x2="10" y2="17" />
                                            <line x1="14" y1="11" x2="14" y2="17" />
                                        </svg>
                                        <div className="session-aborted-card-body">
                                            <strong>Session aborted</strong>
                                            <span className="session-aborted-meta">
                                                {safeString(msg.content)}
                                            </span>
                                            {msg.timestamp && (
                                                <span className="session-aborted-timestamp">
                                                    {(() => {
                                                        try {
                                                            const d = new Date(msg.timestamp);
                                                            return d.toLocaleString(undefined, {
                                                                dateStyle: 'medium',
                                                                timeStyle: 'short',
                                                            });
                                                        } catch {
                                                            return msg.timestamp;
                                                        }
                                                    })()}
                                                    {msg.serverAcknowledged === false && (
                                                        <span title="The server did not acknowledge the delete. The checkpoint may still exist on disk.">
                                                            {' '}· server not reachable
                                                        </span>
                                                    )}
                                                </span>
                                            )}
                                        </div>
                                    </div>
                                )}
                            </div>
                        ))}
                        {isExecuting && (
                            <div className="chat-message assistant">
                                <div className="assistant-row">
                                    <div className="assistant-avatar">AI</div>
                                    <div className="assistant-message assistant-thinking markdown-content">
                                        {/* Timeline stays visible the whole run — including after the
                                            final agent starts streaming — so users keep seeing what
                                            every step did, not just the response text. */}
                                        <ThinkingTimeline
                                            timeline={agentTimeline}
                                            stage={getThinkingStage(streamingAgent, streamingContent)}
                                            hasStreamingContent={Boolean(streamingContent)}
                                            activeSubagents={activeSubagents}
                                            allSubagents={allSubagents}
                                            fallbackStatus={fallbackStatus}
                                        />
                                        {streamingContent && (
                                            <div className="thinking-streaming-content">
                                                <ReactMarkdown remarkPlugins={markdownRemarkPlugins} components={markdownComponents}>{stripEmoji(streamingContent)}</ReactMarkdown>
                                                <span className="streaming-caret" />
                                            </div>
                                        )}
                                    </div>
                                </div>
                            </div>
                        )}
                        {/* HITL pause card — rendered when an `hitl_interrupt`
                            SSE event arrived (or on thread open via the
                            /chat-pending/{id} hydration call). Submitting a
                            decision POSTs to /resume-stream which clears
                            the snapshot server-side. */}
                        {/* Defense in depth: the HITL request is stored globally
                            per workflow but tagged with the thread that produced
                            it. Only render the card when its thread matches the
                            one currently displayed, so a late SSE event or a
                            "New Chat" race never leaks an approval card onto an
                            unrelated thread. When threadId hasn't yet been
                            assigned (legacy state shapes), fall through to the
                            old behaviour so we don't drop a valid prompt. */}
                        {/* Node-failure retry banner — mirrors the HITL card
                            pattern but is driven by a `node_failed` snapshot.
                            Clicking "Retry failed node" reuses handleHitlSubmit
                            with an empty human_input, which the backend routes
                            to its node_failed resume branch (it dispatches on
                            snapshot.reason, not the payload). Rendered here so
                            both the HITL card and this banner never occupy the
                            same slot at once. */}
                        {failureSnapshot &&
                         (!failureSnapshot.threadId || failureSnapshot.threadId === threadId) && (() => {
                            // Two visual variants share the same skeleton so
                            // the layout stays stable as the underlying
                            // snapshot reason changes:
                            //   user_cancelled -> warm amber ("paused")
                            //   node_failed    -> red     ("failure")
                            // All colours come from theme tokens via CSS
                            // classes; no inline hex so light/dark themes
                            // both render correctly.
                            const isCancelled = failureSnapshot.errorType === 'user_cancelled';
                            const variantClass = isCancelled
                                ? 'hitl-card--paused hitl-card--paused-user'
                                : 'hitl-card--paused hitl-card--paused-fail';
                            const label = isCancelled ? 'RUN PAUSED' : 'RUN FAILED';
                            const title = isCancelled ? 'Workflow paused' : 'Workflow paused on failure';
                            const bodyText = isCancelled
                                ? `You stopped the run at ${failureSnapshot.agent || 'this step'}. ` +
                                  `Click Resume to continue with the previous input, or send any message in the chat below to pick up from here with new guidance.`
                                : (failureSnapshot.error || 'Node execution failed.');
                            const primaryLabel = isCancelled ? 'Resume' : 'Retry failed node';
                            const interruptType = isCancelled ? 'user_cancelled' : 'node_failed';
                            const abortRunSession = async () => {
                                // Discards the server-side snapshot so the
                                // paused checkpoint is destroyed. Chat
                                // history is preserved — only the resume
                                // artefact goes away. The next Send starts
                                // a completely fresh workflow run.
                                //
                                // Capture the snapshot's context BEFORE
                                // clearing it so the confirmation message
                                // can reference which node/agent was
                                // discarded. This is a deliberate user
                                // action — surface a clear, professional
                                // system note in the transcript so the
                                // user has an audit trail of what just
                                // happened (silent state changes are
                                // uncomfortable in enterprise UX).
                                const tid = failureSnapshot.threadId;
                                const abortedAgent = failureSnapshot.agent || 'the paused step';
                                const wasCancelled = failureSnapshot.errorType === 'user_cancelled';
                                setFailureSnapshot(null);
                                let serverAcknowledged = true;
                                if (tid) {
                                    try {
                                        const res = await fetch(
                                            `${API_BASE}/chat-pending/${encodeURIComponent(tid)}`,
                                            { method: 'DELETE', headers: buildAuthHeaders() },
                                        );
                                        serverAcknowledged = res.ok;
                                    } catch {
                                        serverAcknowledged = false;
                                    }
                                }
                                // Push a system confirmation into the chat
                                // transcript so the user sees an explicit
                                // record of the abort. Rendered by the
                                // `msg.type === 'session-aborted'` branch
                                // below. The timestamp is captured client-
                                // side (the server has no equivalent
                                // authoritative event to emit) and stored
                                // on the message so a scroll-back later
                                // shows when the discard happened.
                                setMessages(prev => [...prev, {
                                    type: 'session-aborted',
                                    content: `Checkpoint at ${abortedAgent} discarded. Next message starts a new run.`,
                                    agent: abortedAgent,
                                    timestamp: new Date().toISOString(),
                                    serverAcknowledged,
                                }]);
                            };
                            return (
                                <div className={`hitl-card ${variantClass}`} role="alert">
                                    <div className="hitl-card-header">
                                        <span className="hitl-card-dot" />
                                        <span>{label}</span>
                                        <span className="hitl-card-agent">
                                            {failureSnapshot.agent || 'Node'}
                                        </span>
                                    </div>
                                    <div className="hitl-card-prompt">
                                        <strong style={{ display: 'block', marginBottom: 4 }}>{title}</strong>
                                        <span>{bodyText}</span>
                                        {failureSnapshot.errorType && !isCancelled && (
                                            <span className="hitl-card-error-tag" title="Error type">
                                                {failureSnapshot.errorType}
                                            </span>
                                        )}
                                    </div>
                                    <div className="hitl-actions">
                                        <button
                                            type="button"
                                            className="hitl-action-btn hitl-action-btn--resume"
                                            disabled={isExecuting}
                                            onClick={async () => {
                                                // Empty human_input = pure retry from the pinned node.
                                                // Any typed guidance goes through handleSend instead.
                                                setFailureSnapshot(null);
                                                await handleHitlSubmit('', {
                                                    threadId: failureSnapshot.threadId,
                                                    interruptType,
                                                    agent: failureSnapshot.agent || '',
                                                    prompt: '',
                                                    question: '',
                                                    options: [],
                                                });
                                            }}
                                            title={isCancelled
                                                ? 'Continue the workflow from where it stopped'
                                                : 'Re-run the failed node and continue downstream'}
                                        >
                                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                <polygon points="6 4 20 12 6 20 6 4" />
                                            </svg>
                                            {primaryLabel}
                                        </button>
                                        <button
                                            type="button"
                                            className="hitl-action-btn hitl-action-btn--abort"
                                            disabled={isExecuting}
                                            onClick={abortRunSession}
                                            title="Discard the paused checkpoint permanently. Chat history is preserved; the next message starts a fresh run."
                                        >
                                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                <path d="M3 6h18" />
                                                <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                                                <path d="M6 6l1 14a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-14" />
                                            </svg>
                                            Abort session
                                        </button>
                                    </div>
                                </div>
                            );
                        })()}
                        {hitlRequest && (!hitlRequest.threadId || hitlRequest.threadId === threadId) && (() => {
                            // --- Structured ask_human card (AskUserQuestion pattern) ---
                            if (hitlRequest.interruptType === 'ask_human') {
                                const customNum = hitlRequest.options.length + 1;
                                return (
                                    <div className="chat-message assistant hitl-message">
                                        <div className="hitl-card">
                                            <div className="hitl-card-header">
                                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                                    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                                                    <circle cx="9" cy="7" r="4" />
                                                </svg>
                                                <span className="hitl-card-agent">{hitlRequest.agent}</span>
                                                <span className="hitl-card-dot" />
                                                <span className="hitl-card-label">has a question</span>
                                            </div>
                                            <div className="hitl-card-prompt">
                                                {hitlRequest.question}
                                            </div>
                                            <div className="hitl-options-list">
                                                {hitlRequest.options.map((opt, i) => (
                                                    <button
                                                        key={i}
                                                        className="hitl-option-btn"
                                                        onClick={() => handleHitlSubmit(opt)}
                                                    >
                                                        <span className="hitl-option-btn-num">{i + 1}</span>
                                                        <span className="hitl-option-btn-text">{opt}</span>
                                                    </button>
                                                ))}
                                            </div>
                                            <div className="hitl-reply-row">
                                                <span className="hitl-custom-num">{customNum}</span>
                                                <textarea
                                                    className="hitl-reply-input"
                                                    placeholder="Or type a custom reply..."
                                                    value={hitlRedirectText}
                                                    rows={2}
                                                    onChange={(e) => setHitlRedirectText(e.target.value)}
                                                    onKeyDown={(e) => {
                                                        if (e.key === 'Enter' && !e.shiftKey && hitlRedirectText.trim()) {
                                                            e.preventDefault();
                                                            handleHitlSubmit(hitlRedirectText.trim());
                                                        }
                                                    }}
                                                />
                                                <button
                                                    className="hitl-reply-btn"
                                                    onClick={() => hitlRedirectText.trim() && handleHitlSubmit(hitlRedirectText.trim())}
                                                    disabled={!hitlRedirectText.trim()}
                                                    title="Send reply (Enter)"
                                                >
                                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                        <line x1="12" y1="19" x2="12" y2="5" />
                                                        <polyline points="5 12 12 5 19 12" />
                                                    </svg>
                                                </button>
                                            </div>
                                        </div>
                                    </div>
                                );
                            }

                            const isTool = hitlRequest.interruptType === 'before_tool';

                            // --- Before tool execution card ---
                            // Compact one-line list. Each row carries
                            //   [#] [agent] → [tool chip] [×]
                            // — no args, no expandable panel — because tool
                            // inputs are noisy and sometimes sensitive.
                            // The textarea below accepts plain English; the
                            // smart parser turns "don't use foo" into a row
                            // drop and "use bar" into a row add (resolving
                            // bar against the tools catalog). Enter applies
                            // the edits to the displayed list but does NOT
                            // submit — the user still picks Approve / Save &
                            // approve / Allow-all / Reject.
                            if (isTool) {
                                const calls = editedToolCalls;
                                const original = hitlRequest.pendingToolCalls || [];
                                const multi = calls.length > 1;
                                const hasEdits = JSON.stringify(calls.map((c) => c?.name || '')) !==
                                                 JSON.stringify(original.map((c) => c?.name || ''));
                                const applyTextEdits = () => {
                                    const parsed = parseToolEdits(hitlRedirectText, calls, toolCatalog);
                                    setEditedToolCalls(parsed.nextCalls);
                                    setHitlRedirectText(parsed.leftoverText);
                                    setToolEditError(
                                        parsed.unknownNames.length
                                            ? `Tool ${parsed.unknownNames.map((n) => `'${n}'`).join(', ')} not found in catalog`
                                            : '',
                                    );
                                };
                                return (
                                    <div className="chat-message assistant hitl-message">
                                        <div className="hitl-card hitl-card--tool">
                                            <div className="hitl-card-header">
                                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                    <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
                                                </svg>
                                                <span className="hitl-card-agent">{hitlRequest.agent}</span>
                                                <span className="hitl-card-dot" />
                                                <span className="hitl-card-label">
                                                    {calls.length === 0
                                                        ? 'no tools selected'
                                                        : multi
                                                            ? `wants to call ${calls.length} tools`
                                                            : 'wants to call a tool'}
                                                </span>
                                            </div>
                                            <ol className="hitl-tool-list hitl-tool-list--compact">
                                                {calls.map((tc, idx) => (
                                                    <li key={tc?.id || `${tc?.name || 'tool'}-${idx}`} className="hitl-tool-line">
                                                        <span className="hitl-tool-list-num">{idx + 1}</span>
                                                        <span className="hitl-tool-line-agent">{hitlRequest.agent}</span>
                                                        <svg className="hitl-tool-line-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                                            <line x1="5" y1="12" x2="19" y2="12" />
                                                            <polyline points="13 6 19 12 13 18" />
                                                        </svg>
                                                        <span className="hitl-tool-line-name">{tc?.name || 'a tool'}</span>
                                                        <button
                                                            type="button"
                                                            className="hitl-tool-line-remove"
                                                            onClick={() => setEditedToolCalls((c) => c.filter((_, i) => i !== idx))}
                                                            title={`Don't run ${tc?.name || 'this tool'}`}
                                                        >
                                                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
                                                                <line x1="18" y1="6" x2="6" y2="18" />
                                                                <line x1="6" y1="6" x2="18" y2="18" />
                                                            </svg>
                                                        </button>
                                                    </li>
                                                ))}
                                            </ol>
                                            <div className="hitl-tool-actions hitl-tool-actions--grid">
                                                <button
                                                    className="hitl-action-btn hitl-action-btn--allow"
                                                    onClick={() => submitWithToolEdits({ persist: false })}
                                                    disabled={calls.length === 0}
                                                    title={calls.length === 0
                                                        ? 'No tools to approve — add one or reject'
                                                        : 'Approve and run the listed tools for this turn only'}
                                                >
                                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                        <polyline points="20 6 9 17 4 12" />
                                                    </svg>
                                                    Approve
                                                </button>
                                                <button
                                                    className="hitl-action-btn hitl-action-btn--save"
                                                    onClick={() => submitWithToolEdits({ persist: true })}
                                                    disabled={!hasEdits || calls.length === 0}
                                                    title={hasEdits
                                                        ? "Save these tool changes to the agent permanently, then approve"
                                                        : "Make a change to enable — saves the new tool list permanently"}
                                                >
                                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                                        <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
                                                        <polyline points="17 21 17 13 7 13 7 21" />
                                                        <polyline points="7 3 7 8 15 8" />
                                                    </svg>
                                                    Save &amp; approve
                                                </button>
                                                <button
                                                    className="hitl-action-btn hitl-action-btn--allow-all"
                                                    onClick={() => {
                                                        sessionAutoApproveRef.current = true;
                                                        submitWithToolEdits({ persist: false });
                                                    }}
                                                    disabled={calls.length === 0}
                                                    title="Approve this turn AND auto-approve every future tool call for the rest of this session. Edits made here apply only to this turn."
                                                >
                                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                        <polyline points="20 6 9 17 4 12" />
                                                        <polyline points="20 10 9 21 4 16" />
                                                    </svg>
                                                    Allow all this session
                                                </button>
                                                <button
                                                    className="hitl-action-btn hitl-action-btn--no"
                                                    onClick={() => handleHitlSubmit('reject')}
                                                    title="Reject these tool calls"
                                                >
                                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                        <line x1="18" y1="6" x2="6" y2="18" />
                                                        <line x1="6" y1="6" x2="18" y2="18" />
                                                    </svg>
                                                    Reject
                                                </button>
                                            </div>
                                            {/* Enter applies parse → list edit; it does NOT submit. */}
                                            <div className="hitl-reply-row">
                                                <textarea
                                                    ref={(el) => {
                                                        if (!el) return;
                                                        // The ref fires on every render; skip the layout-thrashing
                                                        // height='auto' → scrollHeight measurement when the value
                                                        // hasn't changed (e.g. other panel state re-rendered).
                                                        if (el._lastVal === el.value) return;
                                                        el._lastVal = el.value;
                                                        const maxPx = 7 * 22;
                                                        const minPx = 22;
                                                        if (!el.value) {
                                                            el.style.height = minPx + 'px';
                                                            el.style.overflowY = 'hidden';
                                                            return;
                                                        }
                                                        el.style.height = 'auto';
                                                        el.style.height = Math.min(Math.max(el.scrollHeight, minPx), maxPx) + 'px';
                                                        el.style.overflowY = el.scrollHeight > maxPx ? 'auto' : 'hidden';
                                                    }}
                                                    className="hitl-reply-input hitl-reply-input--autosize"
                                                    placeholder="Edit the list — e.g. ‘don’t use X’, ‘also use Y’…"
                                                    value={hitlRedirectText}
                                                    rows={1}
                                                    onChange={(e) => setHitlRedirectText(e.target.value)}
                                                    onKeyDown={(e) => {
                                                        if (e.key === 'Enter' && !e.shiftKey && hitlRedirectText.trim()) {
                                                            e.preventDefault();
                                                            applyTextEdits();
                                                        }
                                                    }}
                                                />
                                                <button
                                                    className="hitl-reply-btn"
                                                    onClick={() => hitlRedirectText.trim() && applyTextEdits()}
                                                    disabled={!hitlRedirectText.trim()}
                                                    title="Apply edits to the tool list (Enter)"
                                                >
                                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                        <line x1="12" y1="19" x2="12" y2="5" />
                                                        <polyline points="5 12 12 5 19 12" />
                                                    </svg>
                                                </button>
                                            </div>
                                            {toolEditError && (
                                                <div className="hitl-add-error">{toolEditError}</div>
                                            )}
                                        </div>
                                    </div>
                                );
                            }

                            // --- After agent response card (with optional numbered options) ---
                            const { question, options } = parseHitlOptions(hitlRequest.prompt);
                            const hasOptions = options.length > 0;
                            const customOptionNum = options.length + 1;
                            return (
                                <div className="chat-message assistant hitl-message">
                                    <div className="hitl-card">
                                        <div className="hitl-card-header">
                                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                                <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                                                <circle cx="9" cy="7" r="4" />
                                            </svg>
                                            <span className="hitl-card-agent">{hitlRequest.agent}</span>
                                            <span className="hitl-card-dot" />
                                            <span className="hitl-card-label">waiting for your input</span>
                                        </div>
                                        <div className="hitl-card-prompt markdown-content">
                                            <ReactMarkdown remarkPlugins={markdownRemarkPlugins} components={markdownComponents}>
                                                {stripEmoji(hasOptions ? question : hitlRequest.prompt)}
                                            </ReactMarkdown>
                                        </div>
                                        {hasOptions && (
                                            <div className="hitl-options-list">
                                                {options.map((opt, i) => (
                                                    <button
                                                        key={i}
                                                        className="hitl-option-btn"
                                                        onClick={() => handleHitlSubmit(opt)}
                                                    >
                                                        <span className="hitl-option-btn-num">{i + 1}</span>
                                                        <span className="hitl-option-btn-text">{opt}</span>
                                                    </button>
                                                ))}
                                            </div>
                                        )}
                                        {/* Allow / Reject controls — these
                                            map to ``approve`` / ``reject``
                                            in _classify_decision so the
                                            engine continues / cancels the
                                            run unambiguously. The textbox
                                            below is for free-form edits. */}
                                        {!hasOptions && (
                                            <div className="hitl-actions">
                                                <button
                                                    className="hitl-action-btn hitl-action-btn--allow"
                                                    onClick={() => handleHitlSubmit('approve')}
                                                    title="Approve and continue to the next step"
                                                >
                                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                        <polyline points="20 6 9 17 4 12" />
                                                    </svg>
                                                    Allow &amp; continue
                                                </button>
                                                <button
                                                    className="hitl-action-btn hitl-action-btn--no"
                                                    onClick={() => handleHitlSubmit('reject')}
                                                    title="Reject and end the run"
                                                >
                                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                        <line x1="18" y1="6" x2="6" y2="18" />
                                                        <line x1="6" y1="6" x2="18" y2="18" />
                                                    </svg>
                                                    Reject
                                                </button>
                                            </div>
                                        )}
                                        <div className="hitl-reply-row">
                                            {hasOptions && (
                                                <span className="hitl-custom-num">{customOptionNum}</span>
                                            )}
                                            <textarea
                                                ref={(el) => {
                                                    if (!el) return;
                                                    // Auto-grow: shrink first to recompute
                                                    // scrollHeight cleanly, then expand to
                                                    // fit content up to a 7-line ceiling.
                                                    el.style.height = 'auto';
                                                    const maxPx = 7 * 22; // ~22px per line
                                                    el.style.height = Math.min(el.scrollHeight, maxPx) + 'px';
                                                    el.style.overflowY = el.scrollHeight > maxPx ? 'auto' : 'hidden';
                                                }}
                                                className="hitl-reply-input hitl-reply-input--autosize"
                                                placeholder={hasOptions ? 'Or type a custom reply...' : 'Or type a custom edit / instruction…'}
                                                value={hitlRedirectText}
                                                rows={1}
                                                onChange={(e) => setHitlRedirectText(e.target.value)}
                                                onKeyDown={(e) => {
                                                    if (e.key === 'Enter' && !e.shiftKey && hitlRedirectText.trim()) {
                                                        e.preventDefault();
                                                        handleHitlSubmit(hitlRedirectText.trim());
                                                    }
                                                }}
                                                autoFocus={!hasOptions}
                                            />
                                            <button
                                                className="hitl-reply-btn"
                                                onClick={() => hitlRedirectText.trim() && handleHitlSubmit(hitlRedirectText.trim())}
                                                disabled={!hitlRedirectText.trim()}
                                                title="Send reply (Enter)"
                                            >
                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                                    <line x1="12" y1="19" x2="12" y2="5" />
                                                    <polyline points="5 12 12 5 19 12" />
                                                </svg>
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            );
                        })()}
                        <div ref={messagesEndRef} />
                    </>
                )}
            </div>

            <div className="chat-input-container">
                {hasUploadingAttachment && (
                    // Indeterminate progress: fetch() can't expose real upload
                    // bytes, and for scanned PDFs most of the wait is server-side
                    // OCR (not bytes-on-wire). Showing "0%" was misleading. An
                    // animated stripe + "Processing…" label honestly tells the
                    // user "we're working on it" without lying about a number.
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                        <style>{`
                            @keyframes chatAttachStripe {
                                0%   { background-position: 0 0; }
                                100% { background-position: 40px 0; }
                            }
                        `}</style>
                        <div style={{
                            flex: 1,
                            height: 6,
                            background: '#e5e7eb',
                            borderRadius: 999,
                            overflow: 'hidden',
                        }}>
                            <div style={{
                                width: '100%',
                                height: '100%',
                                borderRadius: 999,
                                backgroundImage: 'repeating-linear-gradient(45deg, #3b82f6 0 10px, #60a5fa 10px 20px)',
                                backgroundSize: '40px 40px',
                                animation: 'chatAttachStripe 1s linear infinite',
                            }} />
                        </div>
                        <span style={{
                            fontSize: 11,
                            color: '#3b82f6',
                            fontWeight: 500,
                            textAlign: 'right',
                        }}>Processing…</span>
                    </div>
                )}
                {attachments.length > 0 && (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 6 }}>
                        {attachments.map(a => {
                            const sty = ATTACH_CHIP_STYLE[a.status] || ATTACH_CHIP_STYLE.ready;
                            const sizeBadge = a.status === 'ready' && a.file_size
                                ? `(${_formatFileSize(a.file_size)})`
                                : a.status === 'uploading'
                                    // No fake percentage — fetch() can't expose
                                    // real progress and OCR happens server-side.
                                    ? '…'
                                    : '';
                            const warnings = Array.isArray(a.warnings) ? a.warnings : [];
                            // Compact chip: single-line, square (4px radius),
                            // 10 px text, no inline counters.  Counters and
                            // warnings collapse into the tooltip + the
                            // Preview modal so the chip stays the width of
                            // a filename instead of growing to ~340 px.
                            const hoverParts = [];
                            if (a.status === 'error') hoverParts.push(a.block_reason || 'Upload failed');
                            else if (a.status === 'uploading') hoverParts.push('Uploading and parsing…');
                            else {
                                hoverParts.push(`${a.file_name}`);
                                if (sizeBadge) hoverParts.push(sizeBadge);
                                if (a.kind === 'image') {
                                    hoverParts.push('Image asset — referenced by filename in agent prompt');
                                    if (a.parsed_text) hoverParts.push(`${a.parsed_text.length.toLocaleString()} chars described`);
                                } else {
                                    hoverParts.push(`${(a.parsed_text || '').length.toLocaleString()} chars parsed`);
                                    if (a.images_extracted) hoverParts.push(`${a.images_extracted} image${a.images_extracted > 1 ? 's' : ''} OCR'd`);
                                    if (a.tables_extracted) hoverParts.push(`${a.tables_extracted} table${a.tables_extracted > 1 ? 's' : ''} extracted`);
                                    if (a.truncated) hoverParts.push('Text truncated to prompt budget');
                                    if (warnings.length) hoverParts.push(`${warnings.length} warning${warnings.length > 1 ? 's' : ''}`);
                                }
                            }
                            const hasWarn = a.status === 'ready' && warnings.length > 0;
                            return (
                                <span
                                    key={a.id}
                                    title={hoverParts.join(' · ')}
                                    style={{
                                        display: 'inline-flex',
                                        alignItems: 'center',
                                        gap: 5,
                                        padding: '3px 6px 3px 7px',
                                        fontSize: 10.5,
                                        lineHeight: 1.2,
                                        borderRadius: 4,                  // square-ish
                                        border: '1px solid',
                                        borderColor: hasWarn ? '#fde68a' : sty.border,
                                        background: hasWarn ? '#fffbeb' : sty.bg,
                                        color: hasWarn ? '#92400e' : sty.fg,
                                        maxWidth: 240,
                                        height: 22,
                                    }}
                                >
                                    <span aria-hidden="true" style={{ fontSize: 11 }}>{sty.icon}</span>
                                    <span style={{ ...ATTACH_CHIP_LABEL_STYLE, maxWidth: 130 }}>
                                        {a.file_name}
                                    </span>
                                    {a.status === 'ready' && a.parsed_text && (
                                        <button
                                            type="button"
                                            onClick={() => setPreviewAttachmentId(a.id)}
                                            title={hasWarn
                                                ? `Preview · ${warnings.length} warning${warnings.length > 1 ? 's' : ''}`
                                                : (a.kind === 'image' ? 'Preview image description' : 'Preview extracted text')}
                                            aria-label={a.kind === 'image' ? 'Preview image description' : 'Preview extracted text'}
                                            style={{
                                                background: 'none', border: 'none', cursor: 'pointer',
                                                color: hasWarn ? '#b45309' : '#4f46e5',
                                                fontSize: 10, padding: '0 2px',
                                                lineHeight: 1, fontWeight: 600,
                                            }}
                                        >Preview{hasWarn ? ` (${warnings.length})` : ''}</button>
                                    )}
                                    {a.status === 'ready' && a.kind === 'image' && !a.parsed_text && (
                                        <span style={{ fontSize: 10, color: '#6b7280', padding: '0 2px' }}>Image</span>
                                    )}
                                    {a.status === 'error' && a._file && (
                                        <button
                                            type="button"
                                            onClick={() => retryAttachmentWithOcr(a)}
                                            title="Retry parsing this file"
                                            aria-label="Retry"
                                            disabled={isExecuting}
                                            style={{
                                                background: 'none', border: 'none',
                                                cursor: isExecuting ? 'not-allowed' : 'pointer',
                                                color: '#b91c1c', fontSize: 10, padding: '0 2px',
                                                lineHeight: 1, fontWeight: 600,
                                            }}
                                        >Retry</button>
                                    )}
                                    <button
                                        type="button"
                                        onClick={() => removeAttachment(a.id)}
                                        disabled={isExecuting}
                                        style={{
                                            background: 'none', border: 'none',
                                            cursor: isExecuting ? 'not-allowed' : 'pointer',
                                            color: 'inherit', fontSize: 13, padding: 0, lineHeight: 1,
                                            opacity: 0.65,
                                        }}
                                        aria-label={`Remove ${a.file_name}`}
                                    >×</button>
                                </span>
                            );
                        })}
                    </div>
                )}
                {/* Modal preview of extracted text — rendered once for whichever
                    chip the user clicked the eye button on. Sits outside the
                    chip .map so the modal isn't unmounted while attachments
                    re-order due to status changes. */}
                {previewAttachmentId && (() => {
                    const target = attachments.find(a => a.id === previewAttachmentId);
                    if (!target) return null;
                    return (
                        <ExtractedTextPreview
                            open
                            onClose={() => setPreviewAttachmentId(null)}
                            filename={target.file_name}
                            text={target.parsed_text}
                            engine={target.engine}
                            warnings={target.warnings || []}
                            imagesCount={target.images_extracted || 0}
                            tablesCount={target.tables_extracted || 0}
                            cacheHit={!!target.cache_hit}
                        />
                    );
                })()}
                {attachError && (
                    <div style={{ fontSize: 11, color: '#b91c1c', marginBottom: 6 }}>{attachError}</div>
                )}

                {/* Hidden anchor used by the Teams share deep-link handler. */}
                <a ref={teamsLinkRef} style={{ display: 'none' }} rel="noopener noreferrer" />

                <div className="chat-input-wrapper chat-input-wrapper--preview">
                    <input
                        ref={attachInputRef}
                        type="file"
                        multiple
                        accept={CHAT_ATTACH_ACCEPT}
                        style={{ display: 'none' }}
                        onChange={handleFilesPicked}
                    />
                    <button
                        type="button"
                        className="paperclip-btn"
                        onClick={handleAttachClick}
                        disabled={isExecuting || attachments.length >= CHAT_ATTACH_MAX_FILES}
                        title={
                            attachments.length >= CHAT_ATTACH_MAX_FILES
                                ? `At most ${CHAT_ATTACH_MAX_FILES} files per message`
                                : 'Attach document or image (images are saved as assets the agent can reference)'
                        }
                        aria-label="Attach document or image"
                    >
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
                        </svg>
                    </button>
                    <textarea
                        ref={textareaRef}
                        className="chat-input"
                        placeholder={`Ask ${workflowName || 'this workflow'} anything…`}
                        value={message}
                        onChange={(e) => setMessage(e.target.value)}
                        onKeyDown={handleKeyPress}
                        disabled={isExecuting}
                        rows={1}
                    />
                    <button
                        type="button"
                        className={`send-btn${isExecuting ? ' stopping' : ''}`}
                        onClick={isExecuting ? stopGeneration : handleSend}
                        disabled={
                            !isExecuting
                            && !message.trim()
                            && readyAttachments.length === 0
                        }
                        title={isExecuting ? 'Stop generation' : 'Send message'}
                        aria-label={isExecuting ? 'Stop generation' : 'Send message'}
                    >
                        {/* pointer-events:none keeps the SVG from becoming the
                            click target — without it the inner <path>/<rect>
                            swallowed clicks on some browsers, so only Enter
                            sent the message. */}
                        {isExecuting ? (
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style={SVG_NO_POINTER}>
                                <rect x="6" y="6" width="12" height="12" rx="1.5" />
                            </svg>
                        ) : (
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" style={SVG_NO_POINTER}>
                                <path d="M12 19V5M5 12l7-7 7 7" />
                            </svg>
                        )}
                    </button>
                </div>
            </div>
            </div>{/* /.chat-body */}
            </section>
        </div>
    );
}

export default ChatPanel;
