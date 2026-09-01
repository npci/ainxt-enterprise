// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef } from 'react';

/**
 * Shared portal helper for trigger overlays (result modal, schedule editor,
 * transient toasts).
 *
 * Why a portal? Trigger overlays use `position: fixed; inset: 0` to cover
 * the viewport. The platform host wraps Build Studio inside containers that
 * either carry `backdrop-filter` (`.app-topbar`) or scrollable / clipped
 * dashboard panes. Per CSS spec, any ancestor with `transform`, `filter`,
 * `backdrop-filter`, `perspective`, `contain: paint|layout|strict|content`,
 * or `will-change` establishes a new containing block for `position: fixed`
 * descendants — pinning the overlay to that ancestor's box instead of the
 * viewport. The bell modal hit this via `.app-topbar`'s backdrop-filter; the
 * schedule editor (TriggerModal) hit it inside the scrollable dashboard
 * content area. Both bugs disappear once the overlay is portalled to
 * `document.body`.
 *
 * The portal container carries `data-ac` because every selector in this
 * package's CSS is prefixed with `[data-ac]` at build time (see
 * vite.config.js). Without that attribute on the portal root, none of the
 * `.trigger-modal-*` / `.trigger-toast-*` rules would match.
 *
 * Each component instance owns its own DOM node, so React strict-mode's
 * double-mount cycle stays safe — the cleanup detaches the node and the
 * next mount re-attaches it.
 */
export function useTriggerPortalContainer() {
    const ref = useRef(null);
    if (typeof document !== 'undefined' && !ref.current) {
        const el = document.createElement('div');
        el.setAttribute('data-ac', '');
        el.className = 'trigger-portal-root';
        ref.current = el;
    }
    useEffect(() => {
        if (typeof document === 'undefined') return undefined;
        const el = ref.current;
        if (!el) return undefined;
        document.body.appendChild(el);
        return () => {
            if (el.parentNode) el.parentNode.removeChild(el);
        };
    }, []);
    return ref.current;
}
