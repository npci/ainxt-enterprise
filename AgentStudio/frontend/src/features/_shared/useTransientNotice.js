// SPDX-License-Identifier: MIT
import { useEffect, useState } from 'react';

// Auto-clearing notice slot.
// Caller does `setNotice({ kind, text })`; the hook clears it back to null
// after `timeoutMs`. Re-setting before the timer fires resets the countdown
// because the effect re-runs on every new reference.
export function useTransientNotice(timeoutMs = 4000) {
    const [notice, setNotice] = useState(null);
    useEffect(() => {
        if (!notice) return undefined;
        const t = setTimeout(() => setNotice(null), timeoutMs);
        return () => clearTimeout(t);
    }, [notice, timeoutMs]);
    return [notice, setNotice];
}
