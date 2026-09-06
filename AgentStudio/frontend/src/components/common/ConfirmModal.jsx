// SPDX-License-Identifier: MIT
import { useState, useEffect } from 'react';

/**
 * ConfirmModal Component - Custom modal for confirmations
 */
function ConfirmModal({ isOpen, title, message, onConfirm, onCancel, confirmText = 'Delete', confirmStyle = 'danger' }) {
    const [isClosing, setIsClosing] = useState(false);

    useEffect(() => {
        if (isOpen) {
            setIsClosing(false);
        }
    }, [isOpen]);

    const handleClose = () => {
        setIsClosing(true);
        setTimeout(() => {
            onCancel();
        }, 150);
    };

    const handleConfirm = () => {
        setIsClosing(true);
        setTimeout(() => {
            onConfirm();
        }, 150);
    };

    if (!isOpen) return null;

    return (
        <div className={`confirm-modal-overlay ${isClosing ? 'closing' : ''}`} onClick={handleClose}>
            <div className={`confirm-modal ${isClosing ? 'closing' : ''}`} onClick={(e) => e.stopPropagation()}>
                <div className="confirm-modal-header">
                    <h3 className="confirm-modal-title">{title}</h3>
                    <button className="confirm-modal-close" onClick={handleClose}>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M18 6L6 18M6 6l12 12" />
                        </svg>
                    </button>
                </div>
                <p className="confirm-modal-message">{message}</p>
                <div className="confirm-modal-actions">
                    <button className="confirm-modal-btn cancel" onClick={handleClose}>
                        Cancel
                    </button>
                    <button
                        className={`confirm-modal-btn ${confirmStyle}`}
                        onClick={handleConfirm}
                    >
                        {confirmText}
                    </button>
                </div>
            </div>
        </div>
    );
}

export default ConfirmModal;
