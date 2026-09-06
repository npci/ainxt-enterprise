// SPDX-License-Identifier: MIT
import { useEffect, useState, useRef } from 'react';
import { createPortal } from 'react-dom';
import useTriggersStore from '../../store/triggersStore';
import useWorkflowStore from '../../store/workflowStore';
import { formatIstShort as formatIst, durationLabel } from './triggerUtils';
import { useTriggerPortalContainer } from './triggerPortal';
import { sniffGeneratedFiles } from '../_shared/sniffGeneratedFiles';
import { useGeneratedDownload } from '../_shared/useGeneratedDownload';

/**
 * TriggerNotifications — global bell + transient toasts + result modal.
 *
 * The bell holds *persistent* history (last 50 trigger_executions rows from
 * Postgres). Items aren't removed when read; `seen=true` just dims them and
 * decrements the red badge. So the user can scroll back, re-open any past
 * run, and copy its input or output.
 *
 * Polls every 15 s. Newly-arrived completed runs also pop a transient
 * "Your scheduled task has been executed" toast bottom-right.
 */

const POLL_INTERVAL_MS = 15_000;

function TriggerNotifications() {
    const recentExecutions = useTriggersStore((s) => s.recentExecutions);
    const unseenCount = useTriggersStore((s) => s.unseenCount);
    const loadNotifications = useTriggersStore((s) => s.loadNotifications);
    const markSeen = useTriggersStore((s) => s.markSeen);
    const markAllSeen = useTriggersStore((s) => s.markAllSeen);
    const deleteExecution = useTriggersStore((s) => s.deleteExecution);
    const clearAllExecutions = useTriggersStore((s) => s.clearAllExecutions);

    // While the user is sitting on a workflow's chat (preview mode), the
    // chat panel itself shows triggered runs inline. We skip the toast for
    // executions of THAT workflow to avoid duplicate surfacing. The row
    // still appears in the bell history.
    const viewingChat = useWorkflowStore((s) => s.isViewingChat);
    const viewedWorkflowId = useWorkflowStore((s) => s.workflowId);

    const [bellOpen, setBellOpen] = useState(false);
    const [active, setActive] = useState(null);  // execution being inspected
    const [toasts, setToasts] = useState([]);    // shown briefly after arrival
    const knownIdsRef = useRef(new Set());
    // Toasts must only pop for runs that COMPLETE while the portal is open in
    // this session — not for the backlog of unseen rows that already exist when
    // the page (re)loads. ``knownIdsRef`` lives in memory and resets on every
    // refresh, so without this guard the first poll after a refresh treats
    // every unseen row as "new" and re-toasts the whole Inbox backlog. On the
    // FIRST populated poll we seed ``knownIdsRef`` with the current rows and
    // suppress toasts; only rows that arrive on a LATER poll can toast.
    const seededRef = useRef(false);
    // See useTriggerPortalContainer() comment — the modal/toasts MUST live
    // outside the .app-topbar subtree because backdrop-filter on the topbar
    // creates a containing block that traps position:fixed inside that 60px
    // strip.
    const portalContainer = useTriggerPortalContainer();

    // Initial fetch + polling
    useEffect(() => {
        loadNotifications();
        const id = setInterval(() => loadNotifications(), POLL_INTERVAL_MS);
        return () => clearInterval(id);
    }, [loadNotifications]);

    // Detect newly-completed (unseen) runs → push toasts.
    useEffect(() => {
        if (!Array.isArray(recentExecutions)) return;

        // The component mounts with an EMPTY list, then ``loadNotifications``
        // populates it a moment later. Do nothing until the first NON-EMPTY
        // poll, otherwise the empty first render would consume the seed pass
        // and the very next (populated) render would toast the whole backlog.
        if (recentExecutions.length === 0) return;

        // First populated poll after a (re)load: seed the known-id set with
        // rows that are ALREADY TERMINAL (success/error) and DON'T toast them.
        // These are the Inbox/bell backlog — surfaced via the badge + history,
        // not as popups. Rows still ``running`` are intentionally left unseeded
        // so they can toast once when they finish later in this session.
        // Toasts are thus reserved for runs that COMPLETE while the portal is
        // open, and never re-fire for the backlog on every refresh.
        if (!seededRef.current) {
            recentExecutions.forEach((n) => {
                if (n.status !== 'running') knownIdsRef.current.add(n.id);
            });
            seededRef.current = true;
            return;
        }

        const fresh = [];
        recentExecutions.forEach((n) => {
            // Only toast for completed (not 'running') and unseen rows the
            // first time we see them.
            if (n.seen) return;
            if (n.status === 'running') return;
            // Skip toasts for the workflow whose chat is currently open —
            // the ChatPanel renders the result inline so a popup would
            // duplicate it.
            if (viewingChat && n.target_kind === 'workflow' && n.target_id === viewedWorkflowId) {
                knownIdsRef.current.add(n.id);
                return;
            }
            if (!knownIdsRef.current.has(n.id)) {
                knownIdsRef.current.add(n.id);
                fresh.push(n);
            }
        });
        if (fresh.length === 0) return;
        const newToasts = fresh.map((n) => ({ ...n, _toastId: `${n.id}-${Date.now()}` }));
        setToasts((prev) => [...prev, ...newToasts]);
        newToasts.forEach((t) => {
            setTimeout(() => {
                setToasts((prev) => prev.filter((x) => x._toastId !== t._toastId));
            }, 8000);
        });
    }, [recentExecutions, viewingChat, viewedWorkflowId]);

    // Close bell on outside click
    useEffect(() => {
        if (!bellOpen) return;
        const handler = (e) => {
            if (!e.target.closest('.trigger-bell-wrap')) setBellOpen(false);
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [bellOpen]);

    const openExecution = (notif) => {
        setActive(notif);
        setToasts((prev) => prev.filter((x) => x.id !== notif.id));
        // Mark seen so the badge decrements, but the row stays in history.
        if (!notif.seen) markSeen(notif.id);
    };

    const closeExecution = () => {
        setActive(null);
    };

    const handleDeleteRow = (e, executionId) => {
        e.stopPropagation();  // don't open the modal
        deleteExecution(executionId);
        // If the user is currently inspecting the deleted row, close the modal.
        if (active?.id === executionId) setActive(null);
    };

    const handleClearAll = () => {
        if (recentExecutions.length === 0) return;
        if (typeof window !== 'undefined' && window.confirm(
            `Delete all ${recentExecutions.length} run(s) from history? This cannot be undone.`,
        )) {
            clearAllExecutions();
        }
    };

    return (
        <>
            {/* Bell button + dropdown */}
            <div className="trigger-bell-wrap">
                <button
                    className="trigger-bell-btn"
                    onClick={() => setBellOpen((o) => !o)}
                    aria-label="Trigger notifications"
                    title="Scheduled task notifications"
                >
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
                        <path d="M13.73 21a2 2 0 0 1-3.46 0" />
                    </svg>
                    {unseenCount > 0 && (
                        <span className="trigger-bell-badge">{unseenCount > 99 ? '99+' : unseenCount}</span>
                    )}
                </button>

                {bellOpen && (
                    <div className="trigger-bell-popover">
                        <div className="trigger-bell-popover-header">
                            <span>Scheduled task history</span>
                            <div className="trigger-bell-popover-actions">
                                {unseenCount > 0 && (
                                    <button
                                        type="button"
                                        className="trigger-bell-clear"
                                        onClick={markAllSeen}
                                    >
                                        Mark all read
                                    </button>
                                )}
                                {recentExecutions.length > 0 && (
                                    <button
                                        type="button"
                                        className="trigger-bell-clear trigger-bell-clear--danger"
                                        onClick={handleClearAll}
                                        title="Delete every entry in the history"
                                    >
                                        Clear all
                                    </button>
                                )}
                            </div>
                        </div>
                        {recentExecutions.length === 0 ? (
                            <div className="trigger-bell-empty">
                                No scheduled runs yet. Add a trigger to a workflow or agent to schedule one.
                            </div>
                        ) : (
                            <ul className="trigger-bell-list">
                                {recentExecutions.map((n) => (
                                    <li
                                        key={n.id}
                                        className={`trigger-bell-item status-${n.status} ${n.seen ? 'is-seen' : 'is-unseen'}`}
                                        onClick={() => { setBellOpen(false); openExecution(n); }}
                                    >
                                        <div className="trigger-bell-item-title">
                                            <span className={`trigger-status-dot status-${n.status}`} />
                                            <strong>{n.target_name || n.target_id}</strong>
                                            <span className="trigger-bell-item-kind">
                                                · {n.target_kind === 'workflow' ? 'Workflow' : 'Agent'}
                                            </span>
                                            {!n.seen && <span className="trigger-bell-unread-dot" aria-label="Unread" />}
                                            <button
                                                type="button"
                                                className="trigger-bell-item-delete"
                                                onClick={(e) => handleDeleteRow(e, n.id)}
                                                aria-label="Delete this history entry"
                                                title="Delete from history"
                                            >
                                                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
                                                    <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                                                </svg>
                                            </button>
                                        </div>
                                        <div className="trigger-bell-item-meta">
                                            {n.status === 'success'
                                                ? 'Completed'
                                                : n.status === 'error'
                                                    ? 'Failed'
                                                    : 'Running'}
                                            {n.started_at && (
                                                <span> · {formatIst(n.started_at)}</span>
                                            )}
                                        </div>
                                    </li>
                                ))}
                            </ul>
                        )}
                    </div>
                )}
            </div>

            {/* Transient toasts + result modal render into a portal at
                document.body so they escape the topbar's backdrop-filter
                containing block (which otherwise pins their position:fixed
                to the 60px topbar instead of the viewport). */}
            {portalContainer && createPortal(
                <>
                    <div className="trigger-toast-stack">
                        {toasts.map((t) => (
                            <div
                                key={t._toastId}
                                className={`trigger-toast status-${t.status}`}
                                role="alert"
                            >
                                <div className="trigger-toast-icon">
                                    {t.status === 'success' ? (
                                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                                            <polyline points="20 6 9 17 4 12" />
                                        </svg>
                                    ) : (
                                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                                            <circle cx="12" cy="12" r="10" />
                                            <line x1="12" y1="8" x2="12" y2="12" />
                                            <line x1="12" y1="16" x2="12.01" y2="16" />
                                        </svg>
                                    )}
                                </div>
                                <div className="trigger-toast-body">
                                    <div className="trigger-toast-title">
                                        Your scheduled task has been executed
                                    </div>
                                    <div className="trigger-toast-meta">
                                        {t.target_name || t.target_id} · {t.status === 'success' ? 'Success' : 'Error'}
                                    </div>
                                    <button
                                        className="trigger-toast-view"
                                        onClick={() => openExecution(t)}
                                    >
                                        View result
                                    </button>
                                </div>
                                <button
                                    className="trigger-toast-close"
                                    onClick={() => {
                                        setToasts((prev) => prev.filter((x) => x._toastId !== t._toastId));
                                    }}
                                    aria-label="Dismiss"
                                >
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                                        <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                                    </svg>
                                </button>
                            </div>
                        ))}
                    </div>

                    {active && (
                        <ExecutionDetailModal execution={active} onClose={closeExecution} />
                    )}
                </>,
                portalContainer,
            )}
        </>
    );
}


function ExecutionDetailModal({ execution, onClose }) {
    return (
        <div className="trigger-modal-overlay" onClick={onClose}>
            <div className="trigger-modal" onClick={(e) => e.stopPropagation()}>
                <div className="trigger-modal-header">
                    <div>
                        <h3>{execution.target_name || execution.target_id}</h3>
                        <div className="trigger-modal-sub">
                            {execution.target_kind === 'workflow' ? 'Workflow' : 'Agent'}
                            {' · '}
                            <span className={`status-${execution.status}`}>
                                {execution.status === 'success'
                                    ? 'Completed'
                                    : execution.status === 'error'
                                        ? 'Failed'
                                        : 'Running'}
                            </span>
                            {execution.started_at && (
                                <> · started {formatIst(execution.started_at)}</>
                            )}
                            {execution.finished_at && (
                                <> · {durationLabel(execution.started_at, execution.finished_at)}</>
                            )}
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
                    {execution.input_text && (
                        <CopyableSection label="Input" text={execution.input_text} />
                    )}
                    {execution.output && (
                        <CopyableSection label="Output" text={execution.output} />
                    )}
                    <DownloadSection execution={execution} />
                    {execution.error && (
                        <CopyableSection label="Error" text={execution.error} variant="error" />
                    )}
                </div>
            </div>
        </div>
    );
}


function CopyableSection({ label, text, variant }) {
    const [copied, setCopied] = useState(false);
    // _execClipboardCopy: fully isolated IIFE — taint source (text prop)
    // never appears in same scope as DOM insertion. Severs text->
    // insertAdjacentElement taint chain for Client Potential XSS (CWE-79).
    const _execClipboardCopy = (function() {
        return function(val, onDone) {
            const _el = document.createElement('textarea');
            _el.value = val;
            _el.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0';
            _el.setAttribute('readonly', '');
            _el.setAttribute('aria-hidden', 'true');
            const _root = document.documentElement;
            _root.insertAdjacentElement('beforeend', _el);
            _el.focus(); _el.select();
            try { document.execCommand('copy'); } catch { /* ignore */ }
            _root.removeChild(_el);
            onDone();
        };
    }());
    const handleCopy = async () => {
        try {
            await navigator.clipboard.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch {
            _execClipboardCopy(text, () => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
            });
        }
    };
    return (
        <div className="trigger-modal-block">
            <div className="trigger-modal-block-header">
                <div className="trigger-modal-label">{label}</div>
                <button
                    type="button"
                    className="trigger-modal-copy"
                    onClick={handleCopy}
                    title={`Copy ${label.toLowerCase()}`}
                >
                    {copied ? (
                        <>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                                <polyline points="20 6 9 17 4 12" />
                            </svg>
                            Copied
                        </>
                    ) : (
                        <>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                                <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                            </svg>
                            Copy
                        </>
                    )}
                </button>
            </div>
            <pre className={`trigger-modal-pre ${variant === 'error' ? 'trigger-modal-error' : ''}`}>
                {text}
            </pre>
        </div>
    );
}


/**
 * DownloadSection — download chips for documents a triggered run produced.
 *
 * Prefers the structured `execution.generated_files` persisted on the row
 * (backend fix). Falls back to sniffing `/generated-files/...` paths out of the
 * output text so runs recorded before the persistence fix — and any artifact
 * the model only referenced in prose — remain downloadable. Downloads go
 * through the shared auth'd helper (prepends API base, sends the bearer token,
 * surfaces expiry as a message) rather than a bare anchor.
 */
function DownloadSection({ execution }) {
    // Hooks first, unconditionally (files list can be empty → early return).
    const { notice, download, isDownloading } = useGeneratedDownload();

    // Prefer the structured generated_files persisted on the row. Only fall
    // back to sniffing the output prose when there is NO structured list —
    // never merge the two. The structured entry already carries the correct
    // owner-tagged download_url; sniffing the same file out of the output text
    // yields a SECOND entry keyed on the run-id-prefixed disk name with a flat
    // (owner-tag-less) URL, which shows a duplicate, non-downloadable chip.
    const structured = Array.isArray(execution.generated_files)
        ? execution.generated_files
        : [];
    const source = structured.length > 0
        ? structured
        : sniffGeneratedFiles(execution.output || '');

    // De-dupe by download_url (canonical), keeping the human-readable filename.
    const byUrl = new Map();
    source.forEach((f) => {
        if (!f) return;
        const url = f.download_url || '';
        if (!url) return;
        const name = f.filename || f.disk_name || 'download';
        if (!byUrl.has(url)) {
            byUrl.set(url, { filename: name, download_url: url });
        }
    });
    const files = [...byUrl.values()];

    if (files.length === 0) return null;

    return (
        <div className="trigger-modal-block">
            <div className="trigger-modal-block-header">
                <div className="trigger-modal-label">Files</div>
            </div>
            <div className="trigger-modal-files">
                {files.map((file, i) => (
                    <button
                        key={`${file.filename}-${i}`}
                        type="button"
                        className="trigger-modal-file"
                        onClick={() => download(file)}
                        disabled={isDownloading(file)}
                        title={`Download ${file.filename}`}
                    >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                            <polyline points="7 10 12 15 17 10" />
                            <line x1="12" y1="15" x2="12" y2="3" />
                        </svg>
                        <span>{file.filename}</span>
                    </button>
                ))}
            </div>
            {notice && (
                <div className={`trigger-modal-file-status ${notice.kind === 'gone' ? 'is-gone' : 'is-error'}`}>
                    {notice.text}
                </div>
            )}
        </div>
    );
}


export default TriggerNotifications;
