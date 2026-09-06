// SPDX-License-Identifier: MIT
// Client-side persistence for Build Studio *UI view-state* only — which editor
// is open, the active chat thread, and unsent composer drafts. The DB remains
// the single source of truth for all real data (workflows, agents, chat
// history, config); these keys just let a page reload land the user back where
// they were. Mirrors the existing `abstudio.activeSection` convention.
//
// Keys are namespaced by the authenticated user id (resolved once from
// `/auth/me`, same source of truth the sidebar Chat/Buddy use) so that on a
// shared browser one user never reads another's open-editor pointer or unsent
// draft text. Before the id resolves — or in standalone dev where there is no
// auth — we fall back to an `anon` namespace so the feature still works.
import { platformFetch } from '../config/api';
import { setLocalData, getLocalData, removeLocalData } from './storageUtils';

let userNamespace = 'anon';
let resolvePromise = null;

// Cached identity fields from `/auth/me`, populated by the same one-shot fetch
// that resolves the storage namespace. `department` drives the KB uploader's
// "Restricted to your department" banner and `can_approve` gates the approver
// department multi-select. Empty/false until the fetch resolves.
let currentUser = { id: null, department: '', canApprove: false, role: '' };

// Read the cached identity. Synchronous — returns the `anon` defaults until
// `ensureUserNamespace()` resolves. Callers that render on the identity should
// re-read after awaiting `ensureUserNamespace()` (see useCurrentUser hook).
export function getCurrentUser() {
    return currentUser;
}

// Resolve the current user id once and cache it. Safe to call repeatedly; only
// the first call hits the network. Callers that need namespaced reads on mount
// should `await ensureUserNamespace()` first so keys don't briefly resolve under
// `anon` and miss the user's stored state.
export function ensureUserNamespace() {
    if (resolvePromise) return resolvePromise;
    // Bound the wait so a slow/unreachable /auth/me can never stall the mount
    // restore that awaits this — fall back to the `anon` namespace instead.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    resolvePromise = platformFetch('/auth/me', { signal: controller.signal })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
            if (data && data.id != null) {
                userNamespace = String(data.id);
                currentUser = {
                    id: userNamespace,
                    department: data.department || '',
                    canApprove: Boolean(data.can_approve),
                    role: (data.role || '').toLowerCase(),
                };
            }
            return userNamespace;
        })
        .catch(() => userNamespace)
        .finally(() => clearTimeout(timer));
    return resolvePromise;
}

// Start resolving at module load so the namespace is usually ready before any
// editor component mounts and reads/writes a key. App.jsx still awaits it before
// its one-shot restore to be certain.
ensureUserNamespace();

function nsKey(suffix) {
    return `abstudio.${userNamespace}.${suffix}`;
}

function readJson(key) {
    return getLocalData(key);
}

function writeJson(key, value) {
    setLocalData(key, value);
}

function readString(key) {
    const v = getLocalData(key);
    return v !== null ? String(v) : null;
}

function writeString(key, value) {
    setLocalData(String(key), String(value ?? ''));
}

function remove(key) {
    removeLocalData(key);
}

// ── Open-editor pointer ──────────────────────────────────────────────────
// shape: { kind: 'workflow' | 'agent', id: string, mode: 'edit' | 'preview' }
const OPEN_EDITOR_SUFFIX = 'openEditor';

export function loadOpenEditor() {
    return readJson(nsKey(OPEN_EDITOR_SUFFIX));
}

// Synchronous, namespace-agnostic check for whether ANY user has a stored
// open-editor pointer. Used at first render to decide whether to show a neutral
// loading state (avoiding a dashboard flash) while the async restore resolves.
// The subsequent DB re-fetch + auth still gate what actually opens.
export function hasStoredOpenEditor() {
    try {
        for (let i = 0; i < localStorage.length; i += 1) {
            const key = localStorage.key(i);
            if (key && key.startsWith('abstudio.') && key.endsWith(`.${OPEN_EDITOR_SUFFIX}`)) {
                return true;
            }
        }
    } catch {
        // storage unavailable
    }
    return false;
}

export function saveOpenEditor(pointer) {
    writeJson(nsKey(OPEN_EDITOR_SUFFIX), pointer);
}

export function clearOpenEditor() {
    remove(nsKey(OPEN_EDITOR_SUFFIX));
}

// ── Active chat thread per editor ────────────────────────────────────────
// Synchronous writes so a reload never drops the value. Reads check both
// the resolved namespace and the `anon` fallback (in case the save fired
// before /auth/me resolved).
export function loadActiveThread(kind, editorId) {
    if (!editorId) return null;
    return readString(nsKey(`thread.${kind}.${editorId}`))
        || readString(`abstudio.anon.thread.${kind}.${editorId}`);
}

export function saveActiveThread(kind, editorId, threadId) {
    if (!editorId || !threadId) return;
    writeString(nsKey(`thread.${kind}.${editorId}`), threadId);
}

// ── Unsent composer draft per (editor, thread) ───────────────────────────
// Synchronous writes so a reload never drops the value. Reads check both
// the resolved namespace and the `anon` fallback.
export function loadComposerDraft(kind, editorId, threadId) {
    if (!editorId || !threadId) return '';
    return readString(nsKey(`draft.${kind}.${editorId}.${threadId}`))
        || readString(`abstudio.anon.draft.${kind}.${editorId}.${threadId}`)
        || '';
}

export function saveComposerDraft(kind, editorId, threadId, text) {
    if (!editorId || !threadId) return;
    const key = nsKey(`draft.${kind}.${editorId}.${threadId}`);
    if (text) writeString(key, text);
    else remove(key);
}

export function clearComposerDraft(kind, editorId, threadId) {
    if (!editorId || !threadId) return;
    remove(nsKey(`draft.${kind}.${editorId}.${threadId}`));
}

// ── Selected node per workflow ───────────────────────────────────────────
// Remembers which node's config panel was open so a reload restores it.
export function loadSelectedNode(workflowId) {
    if (!workflowId) return null;
    return readString(nsKey(`selNode.${workflowId}`));
}

export function saveSelectedNode(workflowId, nodeId) {
    if (!workflowId) return;
    const key = nsKey(`selNode.${workflowId}`);
    if (nodeId) writeString(key, nodeId);
    else remove(key);
}
