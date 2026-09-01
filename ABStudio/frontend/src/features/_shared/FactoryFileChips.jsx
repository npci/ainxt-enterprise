// SPDX-License-Identifier: Apache-2.0
// FactoryFileChips — download-chip strip used by the Workflow & Agent
// factory chat overlays. Mirrors the chip UX from AgentRunnerChat but is
// independent of that component so the factory overlays don't drag in
// the full runner's state machine.
//
// Each chip wires through to the shared downloadGeneratedFile() helper,
// which handles the 410 (expired) case and the blob save dance.
//
// Props:
//   files: Array<{ filename, download_url }>
//   onDownload(file): invoked when a chip is clicked
import { API_BASE } from '../../config/api';

const S = {
    strip: {
        display: 'flex', flexWrap: 'wrap', gap: '6px',
        marginTop: '8px',
    },
    btn: {
        display: 'inline-flex', alignItems: 'center', gap: '6px',
        padding: '6px 10px',
        background: '#eef2ff',
        border: '1px solid #c7d2fe',
        borderRadius: '8px',
        color: '#3730a3',
        fontSize: '12px', fontWeight: 600,
        cursor: 'pointer',
        maxWidth: '100%',
        transition: 'background 120ms ease, border-color 120ms ease',
    },
    name: {
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        maxWidth: '220px',
    },
};

function FactoryFileChips({ files, onDownload }) {
    if (!Array.isArray(files) || files.length === 0) return null;
    return (
        <div style={S.strip}>
            {files.map((f, i) => (
                <button
                    key={f.download_url || i}
                    type="button"
                    onClick={() => onDownload(f)}
                    style={S.btn}
                    title={`Download ${f.filename}`}
                    onMouseEnter={(e) => {
                        e.currentTarget.style.background    = '#e0e7ff';
                        e.currentTarget.style.borderColor   = '#a5b4fc';
                    }}
                    onMouseLeave={(e) => {
                        e.currentTarget.style.background    = '#eef2ff';
                        e.currentTarget.style.borderColor   = '#c7d2fe';
                    }}
                >
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                        <polyline points="14 2 14 8 20 8" />
                    </svg>
                    <span style={S.name}>{f.filename}</span>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                        <polyline points="7 10 12 15 17 10" />
                        <line x1="12" y1="15" x2="12" y2="3" />
                    </svg>
                </button>
            ))}
        </div>
    );
}

// Exposed so callers can build absolute URLs without re-importing API_BASE.
export const absoluteDownloadUrl = (file) => `${API_BASE}${file.download_url}`;

export default FactoryFileChips;
