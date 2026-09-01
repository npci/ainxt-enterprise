/**
 * PortalMenu — renders dropdown menu content into <body> with fixed
 * coordinates measured from an anchor element, so the menu escapes every
 * ancestor stacking context and `overflow` clip.
 *
 * Used by the Agent Studio Knowledge pickers so their custom menus (domain
 * type-ahead, department multi-select) never get covered by a later
 * `position:relative` card (e.g. "Sample document") the way an in-flow
 * absolutely-positioned menu does.
 *
 * Props:
 *   anchorRef  — ref to the element the menu should align under (the field).
 *   open       — whether the menu is shown.
 *   onRequestClose — called on outside pointerdown (anywhere outside BOTH the
 *                    anchor and the menu). The parent still owns `open` state.
 *   children   — menu content. Given the shared `.kb-scope-menu` styling by the
 *                caller; PortalMenu only handles positioning + the portal.
 *   className  — optional class for the positioned wrapper (defaults to
 *                'kb-scope-menu').
 *   style      — optional extra style merged onto the wrapper.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

export default function PortalMenu({
    anchorRef,
    open,
    onRequestClose,
    children,
    className = 'kb-scope-menu',
    style = {},
}) {
    const [rect, setRect] = useState(null);
    const [container, setContainer] = useState(null);
    const containerRef = useRef(null);
    const menuRef = useRef(null);

    // Portal target = the nearest ABStudio scoped root (`[data-ac]`) that
    // contains the anchor, falling back to <body>. This keeps the scoped
    // `.kb-scope-menu` CSS applied: in the embedded ai-ui build every ABStudio
    // selector is prefixed with `[data-ac]` (see ai-ui/vite.config.js), so a
    // menu portaled OUTSIDE that subtree would render as bare, unstyled text.
    useLayoutEffect(() => {
        if (!open) { setContainer(null); containerRef.current = null; setRect(null); return; }
        const el = anchorRef?.current;
        const root = el?.closest?.('[data-ac]') || document.body;
        containerRef.current = root;
        setContainer(root);
    }, [open, anchorRef]);

    // Position the menu relative to its ACTUAL containing block. For an
    // absolutely-positioned element that is `menuRef.offsetParent` (the nearest
    // positioned/transformed ancestor — here `.build-studio-root`, which sets
    // `transform: translateZ(0)`). Measuring against the offsetParent (NOT the
    // portal container, which may be a static `[data-ac]` wrapper like
    // `.agent-editor-view`) is what makes left/top correct — otherwise the menu
    // drifts to the left/top of the wrong box.
    const reposition = useCallback(() => {
        const el = anchorRef?.current;
        const menu = menuRef.current;
        if (!el || !menu) return;
        const a = el.getBoundingClientRect();

        const op = menu.offsetParent;              // the real containing block
        const useViewport = !op || op === document.body;
        const cRect = useViewport
            ? { left: 0, top: 0, bottom: window.innerHeight }
            : op.getBoundingClientRect();
        const scrollLeft = useViewport ? 0 : (op.scrollLeft || 0);
        const scrollTop = useViewport ? 0 : (op.scrollTop || 0);

        // Flip up when there isn't room below within the containing block, so
        // the menu never spills outside its container.
        const GAP = 4;
        const MENU_MAX = 240;                       // matches .kb-scope-menu
        const spaceBelow = cRect.bottom - a.bottom - GAP;
        const spaceAbove = a.top - cRect.top - GAP;
        const flipUp = spaceBelow < Math.min(MENU_MAX, 160) && spaceAbove > spaceBelow;
        const maxHeight = Math.max(120, Math.min(MENU_MAX, flipUp ? spaceAbove : spaceBelow));

        const left = a.left - cRect.left + scrollLeft;
        const width = a.width;
        const top = flipUp
            ? (a.top - cRect.top + scrollTop - GAP - maxHeight)
            : (a.bottom - cRect.top + scrollTop + GAP);

        setRect({ left, top, width, maxHeight, fixed: useViewport });
    }, [anchorRef]);

    // Measure AFTER the menu node exists in the DOM (so menuRef.offsetParent
    // is resolvable). The menu is rendered first with visibility:hidden, then
    // revealed once `rect` is computed — prevents a one-frame flash at (0,0).
    useLayoutEffect(() => {
        if (open && container) reposition();
    }, [open, container, reposition]);

    useEffect(() => {
        if (!open) return;
        const onScroll = () => reposition();
        window.addEventListener('scroll', onScroll, true);
        window.addEventListener('resize', onScroll);
        return () => {
            window.removeEventListener('scroll', onScroll, true);
            window.removeEventListener('resize', onScroll);
        };
    }, [open, reposition]);

    useEffect(() => {
        if (!open) return;
        function handle(e) {
            const a = anchorRef?.current;
            if (a && a.contains(e.target)) return;
            if (menuRef.current && menuRef.current.contains(e.target)) return;
            onRequestClose?.();
        }
        document.addEventListener('mousedown', handle);
        return () => document.removeEventListener('mousedown', handle);
    }, [open, anchorRef, onRequestClose]);

    if (!open || !container) return null;

    return createPortal(
        <div
            ref={menuRef}
            className={className}
            style={{
                position: rect?.fixed ? 'fixed' : 'absolute',
                left: rect ? rect.left : 0,
                top: rect ? rect.top : 0,
                width: rect ? rect.width : undefined,
                maxHeight: rect ? rect.maxHeight : 240,
                overflowY: 'auto',
                zIndex: 10000,
                // Hidden until measured so it never flashes at the wrong spot.
                visibility: rect ? 'visible' : 'hidden',
                ...style,
            }}
        >
            {children}
        </div>,
        container,
    );
}
