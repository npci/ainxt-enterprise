/**
 * ScopeSelect — a custom (non-native) single-select dropdown used by the
 * Agent Studio Knowledge pickers (KbScopeCascade + KbUploadScopePicker).
 *
 * Why not a native <select>?
 * --------------------------
 * A native <select>'s open option list is rendered by the OS, so its hover
 * highlight and typography cannot be styled to match the rest of the page.
 * This component renders the menu with the SAME `.kb-scope-menu` classes the
 * domain type-ahead uses (defined in light-theme.css), so every dropdown in
 * the Knowledge section highlights on hover and looks identical.
 *
 * The menu is rendered through <PortalMenu>, which portals it to <body> with
 * fixed coordinates so it floats above later `position:relative` cards (e.g.
 * "Sample document") instead of being covered by them.
 *
 * Props:
 *   value        — currently-selected option value (string) or ''.
 *   onChange     — (nextValue: string) => void. Called with '' when the
 *                  optional placeholder-with-empty-value entry is chosen.
 *   options      — [{ value, label }]. `label` is what renders in the row.
 *   placeholder  — shown on the closed control when nothing is selected.
 *   disabled     — greys/locks the control.
 *   allowEmpty   — when true, an extra "emptyLabel" row (value '') is shown
 *                  so the user can clear the selection (used by the optional
 *                  Document picker: "— Any document in this scope —").
 *   emptyLabel   — label for that clear-row (defaults to placeholder).
 *   emptyText    — text shown inside the menu when options is empty.
 */
import { useEffect, useRef, useState } from 'react';
import PortalMenu from './PortalMenu';

const CONTROL_STYLE = {
    position: 'relative',
    width: '100%',
    background: 'white',
    border: '1px solid #e5e7eb',
    borderRadius: 6,
    padding: '8px 32px 8px 12px',
    fontSize: 12,
    outline: 'none',
    textAlign: 'left',
    minHeight: 35,
    lineHeight: '18px',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
};

function Chevron() {
    return (
        <svg
            aria-hidden="true"
            width="14"
            height="14"
            viewBox="0 0 20 20"
            fill="none"
            style={{
                position: 'absolute',
                right: 10,
                top: '50%',
                transform: 'translateY(-50%)',
                pointerEvents: 'none',
                color: '#9ca3af',
            }}
        >
            <path d="M6 8l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
    );
}

export default function ScopeSelect({
    value = '',
    onChange,
    options = [],
    placeholder = 'Select',
    disabled = false,
    allowEmpty = false,
    emptyLabel = '',
    emptyText = 'No options available',
}) {
    const [open, setOpen] = useState(false);
    const ctrlRef = useRef(null);

    // Close if the control becomes disabled while open.
    useEffect(() => { if (disabled && open) setOpen(false); }, [disabled, open]);

    const selected = options.find(o => String(o.value) === String(value));
    const label = selected ? selected.label : '';
    const isEmpty = options.length === 0;

    function pick(next) {
        onChange?.(next);
        setOpen(false);
    }

    return (
        <div style={{ position: 'relative' }}>
            <button
                ref={ctrlRef}
                type="button"
                disabled={disabled}
                aria-haspopup="listbox"
                aria-expanded={open}
                onClick={() => { if (!disabled) setOpen(o => !o); }}
                style={{
                    ...CONTROL_STYLE,
                    color: value ? '#374151' : '#9ca3af',
                    cursor: disabled ? 'not-allowed' : 'pointer',
                    background: disabled ? '#f9fafb' : 'white',
                    opacity: disabled ? 0.7 : 1,
                }}
            >
                {label || placeholder}
            </button>
            <Chevron />
            <PortalMenu
                anchorRef={ctrlRef}
                open={open && !disabled}
                onRequestClose={() => setOpen(false)}
            >
                {allowEmpty && (
                    <div
                        role="option"
                        aria-selected={!value}
                        className={'kb-scope-menu__item' + (!value ? ' kb-scope-menu__item--active' : '')}
                        onMouseDown={(e) => { e.preventDefault(); pick(''); }}
                    >
                        {emptyLabel || placeholder}
                    </div>
                )}
                {isEmpty && !allowEmpty && (
                    <div className="kb-scope-menu__empty">{emptyText}</div>
                )}
                {options.map(o => (
                    <div
                        key={String(o.value)}
                        role="option"
                        aria-selected={String(o.value) === String(value)}
                        className={
                            'kb-scope-menu__item'
                            + (String(o.value) === String(value) ? ' kb-scope-menu__item--active' : '')
                        }
                        onMouseDown={(e) => { e.preventDefault(); pick(String(o.value)); }}
                    >
                        {o.label}
                    </div>
                ))}
            </PortalMenu>
        </div>
    );
}
