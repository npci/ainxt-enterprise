// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react';
import { apiFetch } from '../../config/api';

/**
 * Modal that prompts the user for a one-liner describing what an agent should
 * do, calls the backend `/generate-instructions` endpoint (which uses the
 * FACTORY_MODEL), and hands the resulting system prompt back via `onAccept`.
 *
 * Shared between the workflow ConfigPanel and the standalone AgentEditor.
 */
export default function GenerateInstructionsModal({ isOpen, onClose, onAccept }) {
    const [prompt, setPrompt] = useState('');
    const [isGenerating, setIsGenerating] = useState(false);

    if (!isOpen) return null;

    const handleGenerate = async () => {
        const trimmed = prompt.trim();
        if (!trimmed || isGenerating) return;
        setIsGenerating(true);
        try {
            const data = await apiFetch(
                '/generate-instructions',
                { method: 'POST', body: JSON.stringify({ prompt: trimmed }) },
                30000,
            );
            const text = (data?.instructions || '').trim();
            if (!text) throw new Error('The model returned an empty response.');
            onAccept(text);
            setPrompt('');
            onClose();
        } catch (error) {
            console.error('Error generating instructions:', error);
            alert(`Failed to generate instructions: ${error.message}`);
        } finally {
            setIsGenerating(false);
        }
    };

    const handleKeyDown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleGenerate();
        }
        if (e.key === 'Escape' && !isGenerating) {
            onClose();
        }
    };

    return (
        <div className="modal-overlay" onClick={isGenerating ? undefined : onClose}>
            <div
                className="modal-content generate-modal"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="modal-header">
                    <div className="modal-icon generate-icon">
                        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" />
                        </svg>
                    </div>
                    <h3 className="modal-title">Generate Instructions</h3>
                </div>
                <p className="modal-message">
                    Describe what you want this agent to do and AI will generate the system instructions.
                </p>
                <textarea
                    className="generate-prompt-input"
                    placeholder="e.g., A customer support agent that helps with billing questions..."
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                    onKeyDown={handleKeyDown}
                    autoFocus
                    rows={4}
                />
                <div className="modal-actions">
                    <button
                        className="modal-btn modal-btn-cancel"
                        onClick={onClose}
                        disabled={isGenerating}
                    >
                        Cancel
                    </button>
                    <button
                        className="modal-btn modal-btn-generate"
                        onClick={handleGenerate}
                        disabled={!prompt.trim() || isGenerating}
                    >
                        {isGenerating ? (
                            <>
                                <span className="btn-spinner"></span>
                                Generating...
                            </>
                        ) : (
                            'Generate'
                        )}
                    </button>
                </div>
            </div>
        </div>
    );
}
