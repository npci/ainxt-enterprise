// SPDX-License-Identifier: MIT
const STYLE = {
    base: {
        margin: '8px 16px 0',
        padding: '8px 12px',
        borderRadius: '8px',
        fontSize: '13px',
    },
    gone:  { background: '#fff7ed', border: '1px solid #fed7aa', color: '#9a3412' },
    error: { background: '#fef2f2', border: '1px solid #fecaca', color: '#991b1b' },
};

// kind: 'gone' (HTTP 410 — file already consumed) | 'error' (any other failure)
export default function DownloadNotice({ notice }) {
    if (!notice) return null;
    return (
        <div role="status" style={{ ...STYLE.base, ...(STYLE[notice.kind] || STYLE.error) }}>
            {notice.text}
        </div>
    );
}
