// SPDX-License-Identifier: MIT
// Kebab-style overlay menu rendered on top of each template card when
// the optional template editor feature is enabled. The menu absorbs its
// own clicks so the underlying "use template" card click still works for
// any non-menu region.
import { useEffect, useRef, useState } from 'react';
import useTemplateAdminStore from '../../store/templateAdminStore';
import ConfirmModal from '../../components/common/ConfirmModal';

function TemplateCardMenu({ template, onEditGraph, onChanged }) {
    const [open, setOpen] = useState(false);
    const [confirm, setConfirm] = useState(null);   // 'delete' | 'reset' | 'save' | null
    const [busy, setBusy] = useState(false);
    const wrapRef = useRef(null);

    const deleteTemplate = useTemplateAdminStore((s) => s.deleteTemplate);
    const resetTemplate = useTemplateAdminStore((s) => s.resetTemplate);
    const saveToSeed = useTemplateAdminStore((s) => s.saveToSeed);

    // Close the menu when clicking outside.
    useEffect(() => {
        if (!open) return;
        const handler = (e) => {
            if (wrapRef.current && !wrapRef.current.contains(e.target)) {
                setOpen(false);
            }
        };
        window.addEventListener('mousedown', handler);
        return () => window.removeEventListener('mousedown', handler);
    }, [open]);

    // Lift the surrounding card wrapper above neighbouring grid cells while
    // the dropdown is open. Each `.template-card-wrap` is `position: relative`
    // but has no z-index, so neighbouring wrappers paint on top in document
    // order. Bumping it temporarily makes the dropdown win.
    useEffect(() => {
        const parent = wrapRef.current?.parentElement;
        if (!parent) return;
        if (open) {
            parent.style.zIndex = '50';
        } else {
            parent.style.zIndex = '';
        }
    }, [open]);

    const stopAndRun = (fn) => (e) => {
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
        fn && fn();
    };

    const handleDelete = async () => {
        setBusy(true);
        const ok = await deleteTemplate(template.id);
        setBusy(false);
        setConfirm(null);
        if (ok && onChanged) onChanged({ id: template.id, removed: true });
    };

    const handleReset = async () => {
        setBusy(true);
        const restored = await resetTemplate(template.id);
        setBusy(false);
        setConfirm(null);
        if (restored && onChanged) onChanged({ id: template.id, restored });
    };

    const handleSaveToSeed = async () => {
        setBusy(true);
        const saved = await saveToSeed(template.id);
        setBusy(false);
        setConfirm(null);
        if (saved && onChanged) onChanged({ id: template.id, saved });
    };

    return (
        <>
            <div
                ref={wrapRef}
                className="template-card-menu"
                onClick={(e) => e.stopPropagation()}
                style={{
                    position: 'absolute',
                    top: 8,
                    right: 8,
                    // Bump the stacking context so the dropdown paints above
                    // neighbouring grid cards (each card wrapper is
                    // `position: relative`, creating its own context).
                    zIndex: open ? 50 : 4,
                }}
            >
                <button
                    type="button"
                    aria-label={`Template actions for ${template.name}`}
                    title="Template actions"
                    onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        setOpen((v) => !v);
                    }}
                    style={kebabBtnStyle}
                >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                        <circle cx="12" cy="5"  r="1.6" />
                        <circle cx="12" cy="12" r="1.6" />
                        <circle cx="12" cy="19" r="1.6" />
                    </svg>
                </button>

                {open && (
                    <div role="menu" style={menuStyle}>
                        <MenuItem onClick={stopAndRun(() => onEditGraph && onEditGraph(template))}>
                            Edit graph
                        </MenuItem>
                        <div style={dividerStyle} />
                        <MenuItem onClick={stopAndRun(() => setConfirm('save'))}>
                            Save to seed
                        </MenuItem>
                        <MenuItem onClick={stopAndRun(() => setConfirm('reset'))}>
                            Reset to seed
                        </MenuItem>
                        <MenuItem danger onClick={stopAndRun(() => setConfirm('delete'))}>
                            Delete
                        </MenuItem>
                    </div>
                )}
            </div>

            <ConfirmModal
                isOpen={confirm === 'delete'}
                title="Delete template"
                message={`Delete "${template.name}"? The seed in workflow_repo.py is unchanged — you can re-create the row via "Reset to seed" anytime.`}
                onConfirm={handleDelete}
                onCancel={() => !busy && setConfirm(null)}
                confirmText={busy ? 'Deleting…' : 'Delete'}
                confirmStyle="danger"
            />
            <ConfirmModal
                isOpen={confirm === 'reset'}
                title="Reset template"
                message={`Restore "${template.name}" to its baseline definition from workflow_repo.py? Any UI edits will be discarded.`}
                onConfirm={handleReset}
                onCancel={() => !busy && setConfirm(null)}
                confirmText={busy ? 'Resetting…' : 'Reset'}
                confirmStyle="confirm"
            />
            <ConfirmModal
                isOpen={confirm === 'save'}
                title="Save to seed"
                message={`Persist the current state of "${template.name}" to the workflow_repo seed overrides? Future "Reset to seed" calls will restore this saved version, and the template will survive a DB wipe.`}
                onConfirm={handleSaveToSeed}
                onCancel={() => !busy && setConfirm(null)}
                confirmText={busy ? 'Saving…' : 'Save'}
                confirmStyle="confirm"
            />
        </>
    );
}

const kebabBtnStyle = {
    width: 26,
    height: 26,
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    border: '1px solid rgba(15, 23, 42, 0.08)',
    background: 'rgba(255, 255, 255, 0.96)',
    borderRadius: 6,
    color: '#475569',
    cursor: 'pointer',
    boxShadow: '0 1px 2px rgba(15, 23, 42, 0.08)',
};

const menuStyle = {
    position: 'absolute',
    top: 30,
    right: 0,
    width: 168,
    background: '#fff',
    border: '1px solid #e2e8f0',
    borderRadius: 8,
    boxShadow: '0 10px 25px rgba(15, 23, 42, 0.15)',
    padding: 4,
    display: 'grid',
    gap: 2,
    zIndex: 50,
};

const menuItemStyle = {
    textAlign: 'left',
    padding: '8px 10px',
    fontSize: 13,
    border: 'none',
    background: 'transparent',
    borderRadius: 6,
    cursor: 'pointer',
    color: '#1e293b',
    fontFamily: 'inherit',
    transition: 'background 120ms ease',
};

function MenuItem({ children, onClick, danger }) {
    const [hover, setHover] = useState(false);
    return (
        <button
            role="menuitem"
            onClick={onClick}
            onMouseEnter={() => setHover(true)}
            onMouseLeave={() => setHover(false)}
            style={{
                ...menuItemStyle,
                color: danger ? '#b91c1c' : menuItemStyle.color,
                background: hover ? (danger ? '#fef2f2' : '#f1f5f9') : 'transparent',
            }}
        >
            {children}
        </button>
    );
}

const dividerStyle = {
    height: 1,
    background: '#e2e8f0',
    margin: '2px 4px',
};

export default TemplateCardMenu;
