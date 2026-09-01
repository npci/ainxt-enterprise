// SPDX-License-Identifier: Apache-2.0
import { useRef } from 'react';

/**
 * Strip markdown syntax to plain text for sharing.
 * Handles the common subset emitted by AI agents:
 * headings, bold/italic, code blocks, inline code, links, lists, blockquotes, tables.
 */
function stripMarkdown(text) {
    if (!text || typeof text !== 'string') return '';
    return text
        // Fenced code blocks → keep content, drop fences
        .replace(/```[\w]*\n?([\s\S]*?)```/g, (_, code) => code.trim())
        // Headings → plain text
        .replace(/^#{1,6}\s+/gm, '')
        // Bold + italic (*** or ___)
        .replace(/\*{3}(.+?)\*{3}/g, '$1')
        .replace(/_{3}(.+?)_{3}/g, '$1')
        // Bold (** or __)
        .replace(/\*{2}(.+?)\*{2}/g, '$1')
        .replace(/_{2}(.+?)_{2}/g, '$1')
        // Italic (* or _)
        .replace(/\*(.+?)\*/g, '$1')
        .replace(/_(.+?)_/g, '$1')
        // Inline code
        .replace(/`(.+?)`/g, '$1')
        // Links → label only
        .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
        // Images → alt text
        .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1')
        // Blockquotes
        .replace(/^>\s?/gm, '')
        // Unordered list bullets
        .replace(/^[\s]*[-*+]\s+/gm, '• ')
        // Ordered list numbers
        .replace(/^[\s]*\d+\.\s+/gm, (m) => m.trimStart())
        // Table separators (|---|---|)
        .replace(/^\|[-| :]+\|$/gm, '')
        // Table rows → tab-separated
        .replace(/^\|(.+)\|$/gm, (_, row) =>
            row.split('|').map(c => c.trim()).filter(Boolean).join('\t')
        )
        // Horizontal rules
        .replace(/^[-*_]{3,}$/gm, '')
        // Collapse 3+ blank lines to 2
        .replace(/\n{3,}/g, '\n\n')
        .trim();
}

/**
 * Shared share/Teams-deep-link hook.
 *
 * Returns:
 *   teamsLinkRef  — attach to a hidden <a> element in the component's JSX
 *   share(text)   — native share → clipboard fallback
 *   shareToTeams  — opens the Teams desktop client via msteams:// URI
 *
 * Usage:
 *   const { teamsLinkRef, share, shareToTeams } = useShareActions();
 *   ...
 *   <a ref={teamsLinkRef} style={{ display: 'none' }} rel="noopener noreferrer" />
 */
export function useShareActions() {
    const teamsLinkRef = useRef(null);

    function share(text, title = 'Response') {
        const str = stripMarkdown((text || '').toString());
        if (navigator.share) {
            navigator.share({ title, text: str }).catch(() => {});
        } else if (navigator.clipboard) {
            navigator.clipboard.writeText(str).catch(() => {});
        } else {
            try {
                const ta = document.createElement('textarea');
                ta.value = str;
                ta.style.cssText = 'position:fixed;opacity:0';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            } catch { /* ignore */ }
        }
    }

    function shareToTeams(text) {
        const str = stripMarkdown((text || '').toString());
        try {
            const encoded = encodeURIComponent(str);
            const teamsUrl = `msteams:/l/chat/0/0?users=&message=${encoded}`;
            if (teamsLinkRef.current) {
                teamsLinkRef.current.href = teamsUrl;
                teamsLinkRef.current.click();
            }
        } catch {
            share(str);
        }
    }

    return { teamsLinkRef, share, shareToTeams };
}
