// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react';
import { ensureUserNamespace, getCurrentUser } from '../utils/editorPersistence';

/**
 * Exposes the current authenticated user's identity fields, sourced from the
 * one-shot `/auth/me` fetch cached in editorPersistence (same source of truth
 * as the storage namespace, so no extra network round-trip). Re-renders once
 * the fetch resolves; in standalone dev (no auth) fields stay at defaults.
 *
 * @returns {{ id: string|null, department: string, canApprove: boolean }}
 */
export default function useCurrentUser() {
    const [user, setUser] = useState(getCurrentUser);

    useEffect(() => {
        let cancelled = false;
        ensureUserNamespace().then(() => {
            if (!cancelled) setUser(getCurrentUser());
        });
        return () => { cancelled = true; };
    }, []);

    return user;
}
