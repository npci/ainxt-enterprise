// SPDX-License-Identifier: MIT
import { useEffect } from 'react';
import { createPortal } from 'react-dom';
import TriggerSection from './TriggerSection';
import { useTriggerPortalContainer } from './triggerPortal';

/**
 * TriggerModal — opens the TriggerSection in a centered modal so any list
 * row (agent card, workflow card, future surfaces) can hand the user the
 * full schedule editor without leaving the dashboard.
 *
 * Props:
 *   open        : boolean — controls visibility
 *   onClose     : called when the user closes the modal
 *   targetKind  : 'workflow' | 'agent'
 *   targetId    : id of the persisted record
 *   targetName  : optional display label for the header
 *   disabled    : optional disabled message (forwarded to TriggerSection)
 */
function TriggerModal({ open, onClose, targetKind, targetId, targetName, disabled }) {
    // Portal the modal out of the dashboard subtree so the overlay's
    // `position: fixed; inset: 0` always resolves against the viewport. The
    // dashboard's scrollable content area + various filter/transform
    // ancestors were otherwise pinning the overlay inside a child box,
    // which made the modal render half off-screen (see triggerPortal.js).
    const portalContainer = useTriggerPortalContainer();

    useEffect(() => {
        if (!open) return;
        const handler = (e) => {
            if (e.key === 'Escape') onClose();
        };
        document.addEventListener('keydown', handler);
        return () => document.removeEventListener('keydown', handler);
    }, [open, onClose]);

    if (!open || !portalContainer) return null;

    return createPortal(
        <div className="trigger-modal-overlay" onClick={onClose}>
            <div
                className="trigger-modal trigger-modal--editor"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="trigger-modal-header trigger-modal-header--editor">
                    <div className="trigger-modal-header-icon" aria-hidden="true">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                            <circle cx="12" cy="12" r="10" />
                            <polyline points="12 6 12 12 16 14" />
                        </svg>
                    </div>
                    <div className="trigger-modal-header-text">
                        <h3>Triggers — {targetName || (targetKind === 'workflow' ? 'Workflow' : 'Agent')}</h3>
                        <div className="trigger-modal-sub">
                            Schedule this {targetKind === 'workflow' ? 'workflow' : 'agent'} to run automatically · Times in IST
                        </div>
                    </div>
                    <button
                        className="trigger-modal-close"
                        onClick={onClose}
                        aria-label="Close"
                    >
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                        </svg>
                    </button>
                </div>
                <div className="trigger-modal-body">
                    <TriggerSection
                        targetKind={targetKind}
                        targetId={targetId}
                        disabled={disabled}
                        variant="modal"
                    />
                </div>
            </div>
        </div>,
        portalContainer,
    );
}

export default TriggerModal;
