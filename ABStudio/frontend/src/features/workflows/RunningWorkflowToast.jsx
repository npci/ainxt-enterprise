// SPDX-License-Identifier: Apache-2.0
import { createPortal } from 'react-dom';
import { useMemo } from 'react';
import useWorkflowStore from '../../store/workflowStore';
import { useTriggerPortalContainer } from '../triggers/triggerPortal';

/**
 * RunningWorkflowToast — bottom-right toast shown on the dashboard while a
 * workflow execution is still in progress in the (now-hidden) editor.
 *
 * Lives at the dashboard level so the user can navigate away from the
 * editor mid-run without losing visibility of what's happening. Clicking
 * "Open" routes the user back into the editor's preview pane, where the
 * ChatPanel — kept mounted in `useWorkflowStore` — has been streaming the
 * agent's output the whole time.
 *
 * Portalled to document.body via the shared trigger portal so the
 * position:fixed anchor isn't trapped by the topbar's backdrop-filter
 * containing block (same reason the trigger toasts use it).
 */
function RunningWorkflowToast({ onOpen }) {
    const portalContainer = useTriggerPortalContainer();

    const isExecuting = useWorkflowStore((s) => s.isExecuting);
    const workflowName = useWorkflowStore((s) => s.workflowName);
    const workflowId = useWorkflowStore((s) => s.workflowId);
    const currentAgent = useWorkflowStore((s) => s.currentAgent);
    const streamingAgent = useWorkflowStore((s) => s.chatStreamingAgent);
    // Subscribe to only the last entry rather than the full executionLogs
    // array. The array grows on every SSE token and would re-render the
    // toast 60×/sec even when streamingAgent/currentAgent already provide
    // a fresher label.
    const lastLog = useWorkflowStore((s) => {
        const logs = s.executionLogs;
        return logs && logs.length > 0 ? logs[logs.length - 1] : null;
    });

    const activityLabel = useMemo(() => {
        if (streamingAgent) return `${streamingAgent} is responding…`;
        if (currentAgent) return `${currentAgent} is working…`;
        if (lastLog) {
            const text = typeof lastLog === 'string'
                ? lastLog
                : (lastLog.message || lastLog.agent || lastLog.type || '');
            if (text) return String(text).slice(0, 80);
        }
        return 'Running…';
    }, [currentAgent, streamingAgent, lastLog]);

    if (!isExecuting || !workflowId) return null;
    if (!portalContainer) return null;

    const displayName = workflowName && workflowName !== 'New workflow'
        ? workflowName
        : 'Workflow';

    return createPortal(
        <div className="running-workflow-toast-stack">
            <div
                className="running-workflow-toast"
                role="status"
                aria-live="polite"
            >
                <div className="running-workflow-toast-icon" aria-hidden="true">
                    {/* Pulsing halo — keyframes defined in styles/triggers.css. */}
                    <span className="running-workflow-toast-pulse" />
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                        <polygon points="5 3 19 12 5 21 5 3" />
                    </svg>
                </div>
                <div className="running-workflow-toast-body">
                    <div className="running-workflow-toast-title">
                        {displayName} is running
                    </div>
                    <div className="running-workflow-toast-meta" title={activityLabel}>
                        {activityLabel}
                    </div>
                </div>
                <button
                    type="button"
                    className="running-workflow-toast-open"
                    onClick={onOpen}
                    aria-label={`Open ${displayName} chat`}
                >
                    Open
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
                        <path d="M5 12h14M13 6l6 6-6 6" />
                    </svg>
                </button>
            </div>
        </div>,
        portalContainer,
    );
}

export default RunningWorkflowToast;
