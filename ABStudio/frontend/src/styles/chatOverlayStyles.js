// SPDX-License-Identifier: Apache-2.0
/**
 * Shared inline-style primitives for the chat-overlay family of components:
 *   AgentFactoryChat, AgentRunnerChat, WorkflowFactoryChat, SkillFactoryChat
 *
 * These now drive off the project's design tokens (see index.css :root/[data-ac])
 * so the factory chats stay consistent with the rest of the app and pick up any
 * future theme changes for free. Each component can spread these and override
 * individual properties as needed.
 *
 * NOTE: the responsive (mobile bottom-sheet) behaviour and prefers-reduced-motion
 * handling live as [data-ac]-scoped CSS classes in light-theme.css — the panel
 * element carries className="factory-chat-panel" so those rules apply. Inline
 * styles below cover the desktop/base look.
 */

// ---------------------------------------------------------------------------
// Motion tokens — shared easing/durations so every chat animates identically.
// ---------------------------------------------------------------------------
export const motion = {
    ease: [0.16, 1, 0.3, 1],          // framer-motion cubic-bezier tuple
    easeCss: 'cubic-bezier(0.16,1,0.3,1)',
    fast: 0.15,
    base: 0.22,
    slow: 0.32,
};

export const overlay = {
    position: 'fixed', inset: 0, zIndex: 1000,
    background: 'rgba(15,23,42,0.22)',
    backdropFilter: 'blur(10px)',
    WebkitBackdropFilter: 'blur(10px)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    // Guarantee breathing room so the panel is never flush to the viewport
    // edges even at its max height — this is what makes it read as "centered".
    padding: '24px',
    boxSizing: 'border-box',
};

export const panel = {
    width: 'min(720px, 94vw)',
    maxWidth: '94vw',
    // Size to content up to a ceiling, rather than always claiming 80vh — a tall
    // fixed height on a short/wide viewport made the panel look top-pinned even
    // though the overlay centers it. Now short content (the hero) sits compactly
    // centered, and long content grows to at most ~88vh.
    height: 'auto',
    minHeight: 'min(520px, 82vh)',
    maxHeight: 'min(88vh, 820px)',
    background: 'var(--color-surface, #ffffff)',
    border: '1px solid var(--color-border, rgba(203,213,225,0.9))',
    borderRadius: 'var(--radius-xl, 20px)',
    display: 'flex', flexDirection: 'column',
    boxShadow: '0 24px 70px rgba(15,23,42,0.18), 0 1px 2px rgba(15,23,42,0.06)',
    overflow: 'hidden',
    WebkitFontSmoothing: 'antialiased',
    fontFamily: 'var(--font-family-base)',
};

export const header = {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '12px 18px',
    borderBottom: '1px solid var(--color-border-subtle, #eef2f7)',
    background: 'var(--color-surface, #fafbfd)',
    flexShrink: 0,
};

export const iconBadge = {
    width: '28px', height: '28px', borderRadius: 'var(--radius-sm, 8px)',
    background: 'var(--color-accent-bg, #eef2ff)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    color: 'var(--color-accent, #4f46e5)', flexShrink: 0,
    boxShadow: 'none',
};

export const closeBtn = {
    width: '28px', height: '28px',
    border: '1px solid var(--color-border, #e2e8f0)',
    background: 'var(--color-surface, #ffffff)', borderRadius: 'var(--radius-sm, 8px)',
    color: 'var(--color-text-secondary, #64748b)', cursor: 'pointer',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    transition: `background ${motion.base}s ${motion.easeCss}, color ${motion.base}s ${motion.easeCss}, border-color ${motion.base}s ${motion.easeCss}`,
};

export const messagesArea = {
    flex: 1, overflowY: 'auto', padding: '16px',
    display: 'flex', flexDirection: 'column', gap: '10px',
    scrollBehavior: 'smooth',
};

export const thinkingRow = {
    display: 'flex', alignItems: 'center', gap: '10px',
    paddingLeft: '4px',
};

export const spinner = {
    width: '14px', height: '14px', borderRadius: '50%', flexShrink: 0,
    border: '2px solid rgba(148,163,184,0.28)',
    borderTopColor: 'var(--color-accent-border, rgba(99,102,241,0.8))',
    animation: 'spin 0.8s linear infinite',
};

export const userRow = {
    display: 'flex', justifyContent: 'flex-end',
    padding: '2px 0',
};

export const aiRow = {
    display: 'flex', alignItems: 'flex-start',
    padding: '2px 0',
};

export const aiAvatar = {
    width: '24px', height: '24px', borderRadius: 'var(--radius-sm, 8px)', flexShrink: 0,
    background: 'var(--color-accent-bg, #eef2ff)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    color: 'var(--color-accent, #4f46e5)', marginTop: '2px',
};

export const userBubble = {
    maxWidth: '75%', padding: '10px 14px',
    background: 'linear-gradient(135deg, var(--color-accent, #4f46e5), var(--color-accent-dark, #4338ca))',
    borderRadius: '16px 16px 4px 16px',
    fontSize: '13px', color: '#fff',
    lineHeight: 1.55,
    boxShadow: '0 4px 12px rgba(99,102,241,0.18)',
    fontWeight: 450,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
};

export const aiBubble = (isError = false) => ({
    maxWidth: '90%', padding: '10px 14px',
    background: isError ? '#fef2f2' : 'var(--color-surface, #ffffff)',
    border: `1px solid ${isError ? '#fecaca' : 'var(--color-border-subtle, #e8ecf1)'}`,
    borderRadius: '4px 16px 16px 16px',
    fontSize: '13px', color: isError ? '#b91c1c' : 'var(--color-text-primary, #1e293b)',
    boxShadow: 'var(--shadow-sm, 0 1px 3px rgba(15,23,42,0.04))',
    lineHeight: 1.55,
    wordBreak: 'break-word',
});

export const inputArea = {
    display: 'flex', gap: '8px',
    padding: '10px 18px 12px',
    borderTop: '1px solid var(--color-border-subtle, #eef2f7)',
    flexShrink: 0,
    background: 'var(--color-surface, #fafbfd)',
};

export const textarea = {
    flex: 1, resize: 'none',
    minHeight: '38px',
    maxHeight: '160px',
    height: '38px',          // reset each keystroke via autoGrowTextarea()
    overflowY: 'auto',
    background: 'var(--color-surface, #ffffff)',
    border: '1px solid var(--color-border, #e2e8f0)',
    borderRadius: 'var(--radius-md, 12px)',
    padding: '9px 12px',
    color: 'var(--color-text-primary, #0f172a)', fontSize: '13px',
    fontFamily: 'inherit', lineHeight: '1.4',
    outline: 'none',
    transition: `border-color ${motion.base}s ${motion.easeCss}, box-shadow ${motion.base}s ${motion.easeCss}`,
};

/**
 * Auto-grow a textarea to fit its content, capped at maxHeight.
 * Call this from the textarea's onInput handler:
 *   onInput={(e) => autoGrowTextarea(e.target)}
 */
export function autoGrowTextarea(el) {
    if (!el) return;
    el.style.height = '38px';                          // collapse first
    const scrollH = el.scrollHeight;
    el.style.height = Math.min(scrollH, 160) + 'px';  // grow up to maxHeight
}

export const sendBtn = (disabled) => ({
    width: '38px', height: '38px', borderRadius: 'var(--radius-md, 12px)',
    background: disabled ? 'var(--color-surface-2, #f1f5f9)' : 'linear-gradient(135deg, var(--color-accent, #4f46e5), var(--color-accent-dark, #4338ca))',
    border: disabled ? '1px solid var(--color-border, #e2e8f0)' : 'none',
    color: disabled ? 'var(--color-text-muted, #94a3b8)' : '#fff',
    cursor: disabled ? 'not-allowed' : 'pointer',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    flexShrink: 0, alignSelf: 'flex-end',
    transition: `transform ${motion.base}s ${motion.easeCss}, box-shadow ${motion.base}s ${motion.easeCss}, background ${motion.base}s ${motion.easeCss}`,
    boxShadow: disabled ? 'none' : '0 4px 12px rgba(79,70,229,0.2)',
});
