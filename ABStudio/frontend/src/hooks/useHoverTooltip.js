// SPDX-License-Identifier: Apache-2.0
import { useEffect, useId, useRef, useState } from 'react';

const DEFAULT_SHOW_DELAY_MS = 450;
const DEFAULT_ESTIMATED_HEIGHT = 130;

export default function useHoverTooltip({
    enabled = true,
    showDelayMs = DEFAULT_SHOW_DELAY_MS,
    estimatedHeight = DEFAULT_ESTIMATED_HEIGHT,
} = {}) {
    const [visible, setVisible] = useState(false);
    const [placement, setPlacement] = useState('above');
    const anchorRef = useRef(null);
    const timerRef = useRef(null);
    const id = useId();

    const clearTimer = () => {
        if (timerRef.current) {
            clearTimeout(timerRef.current);
            timerRef.current = null;
        }
    };

    const computePlacement = () => {
        const el = anchorRef.current;
        if (!el) return 'above';
        const rect = el.getBoundingClientRect();
        return rect.top >= estimatedHeight ? 'above' : 'below';
    };

    const activate = () => {
        if (!enabled) return;
        clearTimer();
        timerRef.current = setTimeout(() => {
            setPlacement(computePlacement());
            setVisible(true);
            timerRef.current = null;
        }, showDelayMs);
    };

    const deactivate = () => {
        clearTimer();
        setVisible(false);
    };

    const onFocus = (e) => {
        if (e.currentTarget.contains(e.relatedTarget)) return;
        activate();
    };

    const onBlur = (e) => {
        if (e.currentTarget.contains(e.relatedTarget)) return;
        deactivate();
    };

    useEffect(() => () => clearTimer(), []);

    return {
        anchorRef,
        tooltipId: id,
        visible,
        placement,
        anchorProps: {
            ref: anchorRef,
            onMouseEnter: activate,
            onMouseLeave: deactivate,
            onFocus,
            onBlur,
            'aria-describedby': visible ? id : undefined,
        },
    };
}
