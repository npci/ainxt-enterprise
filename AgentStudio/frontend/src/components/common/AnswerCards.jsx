// SPDX-License-Identifier: MIT
import { useState } from 'react';

function AnswerCard({ icon, label, selected, onToggle, disabled, fullWidth }) {
    const [hovered, setHovered] = useState(false);
    return (
        <button
            disabled={disabled}
            onClick={() => onToggle(label)}
            onMouseEnter={() => setHovered(true)}
            onMouseLeave={() => setHovered(false)}
            style={{
                gridColumn: fullWidth ? '1 / -1' : 'auto',
                padding: '9px 11px',
                minHeight: '44px',
                background: selected
                    ? '#eef2ff'
                    : hovered && !disabled
                        ? '#f8fafc'
                        : '#ffffff',
                border: `1.5px solid ${selected ? '#818cf8' : hovered && !disabled ? '#bfdbfe' : '#e2e8f0'}`,
                borderRadius: '12px',
                cursor: disabled ? 'not-allowed' : 'pointer',
                textAlign: 'left',
                transform: hovered && !disabled ? 'translateY(-1px)' : 'none',
                boxShadow: selected
                    ? '0 0 0 2px rgba(99,102,241,0.15)'
                    : hovered && !disabled
                        ? '0 8px 18px rgba(15,23,42,0.08)'
                        : '0 1px 2px rgba(15,23,42,0.04)',
                transition: 'transform 180ms cubic-bezier(0.16,1,0.3,1), box-shadow 180ms ease, border-color 180ms ease, background 180ms ease',
                opacity: disabled ? 0.45 : 1,
                fontFamily: 'inherit',
                display: 'flex',
                alignItems: 'center',
                gap: '9px',
                position: 'relative',
            }}
        >
            <span style={{ fontSize: '16px', display: 'block', lineHeight: 1, flexShrink: 0 }}>{icon}</span>
            <span style={{
                fontSize: '12.5px',
                color: selected ? '#4338ca' : '#334155',
                display: 'block',
                lineHeight: 1.28,
                fontWeight: 600,
                letterSpacing: '0',
                flex: 1,
            }}>{label}</span>
            {selected && (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4f46e5" strokeWidth="3" strokeLinecap="round" style={{ flexShrink: 0 }}>
                    <polyline points="20 6 9 17 4 12" />
                </svg>
            )}
        </button>
    );
}

/**
 * AnswerCards — suggestion chips that support multi-select.
 *
 * When `multiSelect` is true (default), users can toggle multiple chips
 * and press a "Send" button to submit all selected labels joined with " + ".
 * When `multiSelect` is false, clicking a chip immediately fires `onSelect`.
 */
function AnswerCards({ suggestions, onSelect, disabled, multiSelect = true }) {
    const [selected, setSelected] = useState(new Set());
    const [sendHovered, setSendHovered] = useState(false);

    if (!suggestions || suggestions.length === 0) return null;

    // Balanced 2-up grid: when there's an odd number of options, the last card
    // spans the full row so a lone trailing card never looks lopsided.
    const isOdd = suggestions.length % 2 === 1;

    const handleToggle = (label) => {
        if (!multiSelect) {
            onSelect(label);
            return;
        }
        setSelected(prev => {
            const next = new Set(prev);
            if (next.has(label)) next.delete(label);
            else next.add(label);
            return next;
        });
    };

    const handleSendSelected = () => {
        if (selected.size === 0) return;
        const text = Array.from(selected).join(' + ');
        setSelected(new Set());
        onSelect(text);
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
                gap: '8px',
            }}>
                {suggestions.map((s, i) => (
                    <AnswerCard
                        key={i}
                        icon={s.icon}
                        label={s.label}
                        selected={multiSelect && selected.has(s.label)}
                        onToggle={handleToggle}
                        disabled={disabled}
                        fullWidth={isOdd && i === suggestions.length - 1}
                    />
                ))}
            </div>
            {multiSelect && selected.size > 0 && (
                <button
                    onClick={handleSendSelected}
                    onMouseEnter={() => setSendHovered(true)}
                    onMouseLeave={() => setSendHovered(false)}
                    style={{
                        alignSelf: 'flex-end',
                        display: 'flex', alignItems: 'center', gap: '6px',
                        padding: '8px 16px',
                        minHeight: '36px',
                        background: sendHovered ? '#4338ca' : '#4f46e5',
                        border: 'none', borderRadius: '10px',
                        color: '#fff',
                        fontSize: '12.5px', fontWeight: 600,
                        cursor: 'pointer',
                        transform: sendHovered ? 'translateY(-1px)' : 'none',
                        boxShadow: sendHovered
                            ? '0 8px 18px rgba(99,102,241,0.35)'
                            : '0 4px 12px rgba(99,102,241,0.25)',
                        transition: 'transform 150ms ease, box-shadow 150ms ease, background 150ms ease',
                    }}
                >
                    Send {selected.size} selected
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                        <line x1="22" y1="2" x2="11" y2="13" />
                        <polygon points="22 2 15 22 11 13 2 9 22 2" />
                    </svg>
                </button>
            )}
        </div>
    );
}

export default AnswerCards;
