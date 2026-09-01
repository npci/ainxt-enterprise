// SPDX-License-Identifier: Apache-2.0
import { useCallback, useRef, useState } from 'react';
import { downloadGeneratedFile, resolveGeneratedUrl } from './downloadGeneratedFile';
import { useTransientNotice } from './useTransientNotice';

// Shared download orchestration for generated files, used by the Build Studio
// chat surfaces so behaviour is identical everywhere:
//   - always routes clicks through the auth'd `downloadGeneratedFile` helper
//     (never a bare `target="_blank"` navigation that would 401 / show raw
//     JSON / redirect to the SPA dashboard),
//   - ignores a repeat click while the same file is still downloading, so a
//     double-click can't fire two concurrent blob downloads,
//   - surfaces 410 (expired) / error via a transient notice.
//
// `download(file)` accepts a generated-file-shaped object and resolves the
// URL + saved filename from that object rather than from any markdown link
// text, so the saved file always keeps its real name.
//
// Returns: { notice, download, isDownloading }
export function useGeneratedDownload() {
    const [notice, setNotice] = useTransientNotice();
    const [inFlight, setInFlight] = useState(() => new Set());
    // Ref mirror so concurrent synchronous clicks see the latest set without
    // waiting for a re-render (the memoized `download` doesn't close over it).
    const inFlightRef = useRef(inFlight);
    inFlightRef.current = inFlight;

    const download = useCallback(async (file) => {
        if (!file) return;
        const url = resolveGeneratedUrl(file.href || file.download_url || '');
        if (!url) return;
        const filename = file.filename || file.disk_name || 'download';

        if (inFlightRef.current.has(url)) return;
        setInFlight((prev) => {
            const next = new Set(prev);
            next.add(url);
            return next;
        });
        try {
            const result = await downloadGeneratedFile(url, filename);
            if (result.status !== 'ok') {
                setNotice({ kind: result.status, text: result.message });
            }
        } finally {
            setInFlight((prev) => {
                const next = new Set(prev);
                next.delete(url);
                return next;
            });
        }
    }, [setNotice]);

    const isDownloading = useCallback((keyOrFile) => {
        if (!keyOrFile) return false;
        const raw = typeof keyOrFile === 'string'
            ? keyOrFile
            : (keyOrFile.href || keyOrFile.download_url || '');
        const url = resolveGeneratedUrl(raw);
        return url ? inFlight.has(url) : false;
    }, [inFlight]);

    return { notice, download, isDownloading };
}
