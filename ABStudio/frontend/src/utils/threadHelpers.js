// SPDX-License-Identifier: Apache-2.0
/**
 * Shared thread/chat-history helpers used by AgentEditor, AgentRunnerChat,
 * and other agent-chat components.
 *
 * Extracted to avoid duplicating ~50 identical lines in every file that
 * renders the chat-history sidebar.
 */

export function formatRelativeTime(isoTs) {
    if (!isoTs) return '';
    const then = Date.parse(isoTs);
    if (Number.isNaN(then)) return '';
    const deltaMs = Math.max(0, Date.now() - then);
    const mins = Math.floor(deltaMs / 60000);
    if (mins < 1) return 'now';
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h`;
    const days = Math.floor(hrs / 24);
    if (days < 7) return `${days}d`;
    return `${Math.floor(days / 7)}w`;
}

export function getThreadGroup(thread) {
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

export function groupThreads(threadsToGroup) {
    const groups = { Today: [], Yesterday: [], 'Last 7 Days': [], Older: [] };
    threadsToGroup.forEach((t) => { groups[getThreadGroup(t)].push(t); });
    return Object.entries(groups).filter(([, items]) => items.length > 0);
}

export function threadTitle(thread) {
    return thread.title || 'New chat';
}

export function threadPreview(thread) {
    return thread.last_message_preview || 'Continue the conversation';
}

// User messages sent through the Agent / Workflow chat panes are
// composed with one or more attachment blocks prepended to the user's
// typed question. Two prefix shapes exist in the wild:
//
//   1) `[File: <name>]\n<parsed text>` … `\n\nUser question: <text>`
//      (Workflow ChatPanel + Agent editor preview pane)
//   2) `Attached document "<name>":\n---\n<parsed text>\n---` … `\n\n<text>`
//      (AgentRunnerChat overlay)
//
// The whole composed string is persisted server-side so the LLM has the
// full document on subsequent turns — but in the UI we never want to
// re-expose the raw parsed dump on history reload. This helper strips
// the prepended blocks and returns just the user's question plus a
// compact "(n file(s) attached: <names>)" marker, matching the live
// bubble. If the content doesn't match either shape it is returned
// unchanged so we never lose user data.
// Canonical writer for the "(N file(s) attached: <names>)" marker that
// trails a user bubble. Paired with splitFileAttachmentMarker below —
// keep these two in lock-step or history reloads will silently mis-parse.
export function formatFileAttachmentMarker(filenames) {
    const n = filenames.length;
    return `_(${n} file${n === 1 ? '' : 's'} attached: ${filenames.join(', ')})_`;
}

export function sanitizeUserMessageForDisplay(content) {
    if (typeof content !== 'string' || !content) return content || '';

    // Shape 1 — `[File: <name>]` blocks (Workflow ChatPanel + Agent editor)
    if (content.startsWith('[File: ')) {
        const filenames = [];
        let cursor = 0;
        while (content.startsWith('[File: ', cursor)) {
            const close = content.indexOf(']', cursor + 7);
            if (close === -1) break;
            filenames.push(content.slice(cursor + 7, close));
            const nextFile = content.indexOf('\n\n[File: ', close);
            const userQ    = content.indexOf('\n\nUser question: ', close);
            if (userQ !== -1 && (nextFile === -1 || userQ < nextFile)) {
                cursor = userQ + '\n\nUser question: '.length;
                break;
            }
            if (nextFile === -1) return content; // malformed — keep raw
            cursor = nextFile + 2; // skip the "\n\n"
        }
        if (filenames.length > 0) {
            const typed = content.slice(cursor).trim();
            const marker = formatFileAttachmentMarker(filenames);
            return typed ? `${typed}\n\n${marker}` : marker;
        }
    }

    // Shape 2 — `Attached document "<name>":\n---\n...\n---` (AgentRunnerChat)
    if (content.startsWith('Attached document "')) {
        const filenames = [];
        let cursor = 0;
        const HEADER_RE = /^Attached document "([^"]+)":\n---\n/;
        while (true) {
            const slice = content.slice(cursor);
            const head = HEADER_RE.exec(slice);
            if (!head) break;
            filenames.push(head[1]);
            const bodyStart = cursor + head[0].length;
            const fenceEnd = content.indexOf('\n---', bodyStart);
            if (fenceEnd === -1) return content; // malformed — keep raw
            // Advance past the closing fence and the separating blank line.
            cursor = fenceEnd + '\n---'.length;
            if (content.startsWith('\n\n', cursor)) cursor += 2;
        }
        if (filenames.length > 0) {
            let typed = content.slice(cursor).trim();
            // The composer falls back to a "(no question — ...)" stand-in
            // when the user attaches a file without typing — drop that so
            // the rendered bubble matches what the user actually saw live.
            if (typed === '(no question — please review the attached document)') typed = '';
            const marker = formatFileAttachmentMarker(filenames);
            return typed ? `${typed}\n\n${marker}` : marker;
        }
    }

    return content;
}

// Split a user-bubble display string composed by AgentEditor's send-path
// (or restored via sanitizeUserMessageForDisplay) into the user's typed
// text and the list of attached filenames. The marker shape is:
//
//   _(<N> file(s) attached: <name1>, <name2>, ...)_
//
// trailing the typed text, separated by a blank line. Used by the chat
// renderer so the marker can be displayed as a styled chip strip instead
// of leaking the raw markdown italic into a plain-text bubble (which
// would otherwise show "_(1 file attached: foo.xlsx)_" verbatim).
export function splitFileAttachmentMarker(content) {
    if (typeof content !== 'string' || !content) {
        return { text: content || '', filenames: [] };
    }
    // Tolerant of leading/trailing whitespace so a round-tripped history
    // message (which may have been re-trimmed by the persistence layer)
    // still parses cleanly.
    const re = /(?:^|\n\n)_\((\d+)\s+files?\s+attached:\s+([^)]+)\)_\s*$/;
    const m = content.match(re);
    if (!m) return { text: content, filenames: [] };
    const filenames = m[2].split(',').map(s => s.trim()).filter(Boolean);
    const text = content.slice(0, m.index).replace(/\n+$/, '');
    return { text, filenames };
}

export function mapHistoryToUiMessages(historyMessages) {
    return (historyMessages || []).map((msg, idx) => {
        const raw = typeof msg.content === 'string' ? msg.content : String(msg.content ?? '');
        const role = msg.role === 'assistant' ? 'assistant' : 'user';
        const ui = {
            id: `hist-${idx}-${Math.random().toString(36).slice(2, 7)}`,
            role,
            // Hide the prepended `[File: ...]` parsed-text block from the
            // user bubble on reload — the agent still has the full text in
            // its persisted history, but the UI only needs the typed
            // question + a compact "file attached" marker.
            content: role === 'user' ? sanitizeUserMessageForDisplay(raw) : raw,
        };
        // Mirrors the Workflow ChatPanel's history mapping: when an
        // assistant message persisted with attached artifacts (file
        // download cards), restore them here so the chips re-render on
        // thread reload without re-running the agent.
        if (Array.isArray(msg.generated_files) && msg.generated_files.length > 0) {
            ui.generatedFiles = msg.generated_files;
        }
        // Restore the usage footer (model / tokens / cost) on reload.
        if (msg.usage) {
            ui.usage = msg.usage;
        }
        return ui;
    });
}
