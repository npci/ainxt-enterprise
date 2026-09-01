// SPDX-License-Identifier: Apache-2.0
import { useCallback, useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion as fm } from 'framer-motion';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import * as base from '../../styles/chatOverlayStyles';
import { autoGrowTextarea } from '../../styles/chatOverlayStyles';
import { motion as motionTokens } from '../../styles/chatOverlayStyles';
import { stripEmoji } from '../../utils/stripEmoji';
import AnswerCards from '../../components/common/AnswerCards';
import { useTriggerPortalContainer } from '../triggers/triggerPortal';

// Stable plugin reference so ReactMarkdown props don't change identity.
const REMARK_PLUGINS = [remarkGfm];

const FOCUSABLE =
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * FactoryChatShell — the shared chrome for all three "Create with AI" chats
 * (Workflow / Agent / Skill). Owns the overlay, animated panel, header,
 * scrollable message list (steps blocks + markdown bubbles), suggestion chips,
 * input area, and footer slot. Feature-specific UI (pipeline editor, agent
 * confirm, skill preview) is injected via `renderAboveInput` / `footer` /
 * `bodyOverlay`.
 *
 * Accessibility: role="dialog", aria-modal, labelled by the title, Escape to
 * close, focus trap, focus restore on unmount, and an aria-live steps region.
 * Motion: framer-motion enter/exit; respects prefers-reduced-motion via CSS.
 *
 * Props:
 *  - title, subtitle, icon        header content
 *  - onClose()                    close handler
 *  - messages[]                   from useFactoryChatStream
 *  - suggestions[]                chip options (hidden while loading)
 *  - isLoading                    disables input + hides chips
 *  - onSend(text)                 send a typed message
 *  - onChipSelect(text)           send a chip selection
 *  - inputValue, setInputValue    controlled textarea
 *  - inputPlaceholder             placeholder text (stage-aware, caller decides)
 *  - inputDisabled                extra disable (e.g. non-clarifying stages)
 *  - hero                         node shown when messages is empty
 *  - renderMessageExtras(msg)     optional node under an assistant bubble (file chips)
 *  - belowMessages                node rendered after the message list (match cards)
 *  - renderAboveInput             node between messages and input (apply/summary bar)
 *  - footer                       replaces the input area entirely (e.g. trigger panel)
 *  - bodyOverlay                  absolutely-positioned overlay over the body (pipeline editor)
 *  - hideInput                    hide the input area (e.g. success state)
 *  - panelStyle                   extra style merged onto the panel
 */
function FactoryChatShell({
    title,
    subtitle,
    icon,
    onClose,
    messages = [],
    suggestions = [],
    isLoading = false,
    onSend,
    onChipSelect,
    inputValue = '',
    setInputValue,
    inputPlaceholder = 'Type a message…',
    inputDisabled = false,
    hero = null,
    renderMessageExtras,
    belowMessages = null,
    renderAboveInput = null,
    footer = null,
    bodyOverlay = null,
    hideInput = false,
    panelStyle = null,
}) {
    const portalContainer = useTriggerPortalContainer();
    const panelRef = useRef(null);
    const inputRef = useRef(null);
    const messagesEndRef = useRef(null);
    const previouslyFocused = useRef(null);
    const titleId = useRef(`factory-chat-title-${Math.floor(Math.random() * 1e6)}`).current;

    // Remember what had focus so we can restore it when the modal closes.
    useEffect(() => {
        previouslyFocused.current = document.activeElement;
        // Focus the input on open (fallback to the panel).
        const t = setTimeout(() => {
            (inputRef.current || panelRef.current)?.focus?.();
        }, 40);
        return () => {
            clearTimeout(t);
            previouslyFocused.current?.focus?.();
        };
    }, []);

    // Shrink the textarea back to its minimum when the value is cleared
    // (e.g. after the user sends a message and the parent resets inputValue).
    useEffect(() => {
        if (!inputValue && inputRef.current) {
            inputRef.current.style.height = '38px';
        }
    }, [inputValue]);

    // Escape to close + focus trap (Tab cycles within the panel).
    const onKeyDown = useCallback((e) => {
        if (e.key === 'Escape') {
            e.stopPropagation();
            onClose?.();
            return;
        }
        if (e.key !== 'Tab') return;
        const panel = panelRef.current;
        if (!panel) return;
        const nodes = Array.from(panel.querySelectorAll(FOCUSABLE)).filter(
            (n) => n.offsetParent !== null || n === document.activeElement,
        );
        if (nodes.length === 0) return;
        const first = nodes[0];
        const last = nodes[nodes.length - 1];
        if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
        }
    }, [onClose]);

    // Auto-scroll to newest content. Instant while streaming (frequent updates),
    // smooth when idle.
    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({
            behavior: isLoading ? 'auto' : 'smooth',
            block: 'end',
        });
    }, [messages, suggestions, isLoading]);

    const handleTextareaKey = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            const text = inputValue.trim();
            if (text && !isLoading && !inputDisabled) onSend?.(text);
        }
    };

    const handleSendClick = () => {
        const text = inputValue.trim();
        if (text && !isLoading && !inputDisabled) onSend?.(text);
    };

    if (!portalContainer) return null;

    const sendDisabled = !inputValue.trim() || isLoading || inputDisabled;

    return createPortal(
        <AnimatePresence>
            <fm.div
                key="factory-chat-overlay"
                className="factory-chat-overlay"
                style={base.overlay}
                /* Intentionally NO backdrop click-to-close — an accidental click
                   outside the panel must not discard an in-progress build. Close
                   only via the ✕ button or Escape. */
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: motionTokens.base, ease: motionTokens.ease }}
            >
                <fm.div
                    ref={panelRef}
                    className="factory-chat-panel"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby={titleId}
                    tabIndex={-1}
                    style={{ ...base.panel, position: 'relative', ...(panelStyle || {}) }}
                    onClick={(e) => e.stopPropagation()}
                    onKeyDown={onKeyDown}
                    initial={{ opacity: 0, y: 12, scale: 0.985 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: 8, scale: 0.985 }}
                    transition={{ duration: motionTokens.slow, ease: motionTokens.ease }}
                >
                    {/* Header */}
                    <div style={base.header}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', minWidth: 0 }}>
                            <div style={base.iconBadge}>{icon}</div>
                            <div style={{ minWidth: 0 }}>
                                <div id={titleId} style={{ fontWeight: 650, fontSize: '14px', color: 'var(--color-text-primary, #0f172a)' }}>
                                    {title}
                                </div>
                                {subtitle && (
                                    <div style={{ fontSize: '11px', color: 'var(--color-text-secondary, #64748b)', marginTop: '1px' }}>
                                        {subtitle}
                                    </div>
                                )}
                            </div>
                        </div>
                        <button style={base.closeBtn} onClick={onClose} aria-label="Close">
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                        </button>
                    </div>

                    {/* Messages */}
                    <div style={base.messagesArea}>
                        {messages.length === 0 && hero}

                        {messages.map((msg) => {
                            if (msg.type === 'steps') {
                                return <StepsBlock key={msg.id} block={msg} />;
                            }
                            const isUser = msg.role === 'user';
                            return (
                                <div
                                    key={msg.id}
                                    className="factory-chat-msg-enter"
                                    style={isUser ? base.userRow : base.aiRow}
                                >
                                    <div style={isUser ? base.userBubble : base.aiBubble(msg.isError)}>
                                        {isUser ? (
                                            <span style={{ whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{msg.content}</span>
                                        ) : (
                                            <div style={{ lineHeight: 1.5 }} className="agent-chat-md">
                                                <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>
                                                    {stripEmoji(msg.content)}
                                                </ReactMarkdown>
                                                {renderMessageExtras?.(msg)}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            );
                        })}

                        {belowMessages}
                        <div ref={messagesEndRef} />
                    </div>

                    {/* Feature-specific bar between messages and input */}
                    {renderAboveInput}

                    {/* Suggestion chips */}
                    {suggestions.length > 0 && !isLoading && (
                        <div style={{ padding: '6px 16px 12px', flexShrink: 0 }}>
                            <AnswerCards
                                suggestions={suggestions}
                                onSelect={onChipSelect}
                                disabled={isLoading}
                            />
                        </div>
                    )}

                    {/* Input area, or a full footer override */}
                    {footer
                        ? footer
                        : !hideInput && (
                            <div style={base.inputArea}>
                                <textarea
                                    ref={inputRef}
                                    style={base.textarea}
                                    placeholder={inputPlaceholder}
                                    value={inputValue}
                                    onChange={(e) => setInputValue?.(e.target.value)}
                                    onInput={(e) => autoGrowTextarea(e.target)}
                                    onKeyDown={handleTextareaKey}
                                    rows={1}
                                    disabled={isLoading}
                                    aria-label="Message"
                                />
                                <button
                                    style={base.sendBtn(sendDisabled)}
                                    onClick={handleSendClick}
                                    disabled={sendDisabled}
                                    aria-label="Send"
                                >
                                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                        <line x1="22" y1="2" x2="11" y2="13" />
                                        <polygon points="22 2 15 22 11 13 2 9 22 2" />
                                    </svg>
                                </button>
                            </div>
                        )}

                    {/* Absolutely-positioned overlay over the whole body (e.g. pipeline editor) */}
                    {bodyOverlay}
                </fm.div>
            </fm.div>
        </AnimatePresence>,
        portalContainer,
    );
}

// --- Steps block (accumulating progress) ---
function StepsBlock({ block }) {
    if (block.steps.length === 0 && !block.done) {
        return (
            <div style={S.stepsBlock} aria-live="polite">
                <div style={S.stepRow}>
                    <div style={S.stepSpinner} />
                    <span style={S.stepTextActive}>Thinking…</span>
                </div>
            </div>
        );
    }
    return (
        <div style={S.stepsBlock} aria-live="polite">
            {block.steps.map((step, i) => (
                <div key={i} style={S.stepRow}>
                    {step.status === 'active' ? (
                        <div style={S.stepSpinner} />
                    ) : (
                        <div style={S.stepCheck}>
                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none"
                                stroke="#16a34a" strokeWidth="3" strokeLinecap="round">
                                <polyline points="20 6 9 17 4 12" />
                            </svg>
                        </div>
                    )}
                    <span style={step.status === 'active' ? S.stepTextActive : S.stepTextDone}>
                        {step.text}
                    </span>
                </div>
            ))}
        </div>
    );
}

const S = {
    stepsBlock: {
        display: 'flex', flexDirection: 'column', gap: '4px',
        padding: '10px 14px',
        background: 'linear-gradient(135deg, #f8fafc, #f1f5f9)',
        border: '1px solid var(--color-border-subtle, #e2e8f0)',
        borderRadius: 'var(--radius-md, 12px)',
    },
    stepRow: {
        display: 'flex', alignItems: 'center', gap: '10px',
        minHeight: '26px',
    },
    stepSpinner: {
        width: '14px', height: '14px', borderRadius: '50%', flexShrink: 0,
        border: '2px solid rgba(148,163,184,0.2)',
        borderTopColor: 'var(--color-accent, #4f46e5)',
        animation: 'spin 0.7s linear infinite',
    },
    stepCheck: {
        width: '14px', height: '14px', borderRadius: '50%', flexShrink: 0,
        background: 'rgba(22,163,74,0.1)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
    },
    stepTextActive: { fontSize: '12.5px', color: 'var(--color-text-primary, #1e293b)', fontWeight: 550 },
    stepTextDone: { fontSize: '12.5px', color: 'var(--color-text-muted, #94a3b8)', fontWeight: 400 },
};

export default FactoryChatShell;
