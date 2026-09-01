// SPDX-License-Identifier: Apache-2.0
import { useEffect, useMemo, useState } from 'react';
import { API_BASE, PLATFORM_API_BASE } from '../config/api';
import { RECOMMENDED_MODEL } from '../config/models';

/**
 * Status values exposed by useAvailableModels. Exported so consumers can
 * switch on the constant instead of magic strings.
 */
export const MODEL_STATUS = Object.freeze({
    LOADING: 'loading',
    READY:   'ready',
    EMPTY:   'empty',
    ERROR:   'error',
});

const DEFAULT_PROVIDER = 'custom';

// Preferred default when the backend doesn't pin one and the model is
// available in the catalogue. Keeps the long-standing "Claude Sonnet is
// the safe default" behaviour without breaking the grouped-providers UX.

function pickDefaultModel(ids, serverDefault) {
    const recommendedHit = ids.find(
        id => id === RECOMMENDED_MODEL || id.endsWith(`/${RECOMMENDED_MODEL}`)
    );
    return recommendedHit
        || serverDefault
        || ids.find(id => id !== 'auto')
        || ids[0]
        || RECOMMENDED_MODEL;
}

/**
 * ``authFetch`` mirror — no ``Authorization`` header; the browser's httpOnly
 * ``auth_token`` cookie is sent automatically when ``credentials: 'include'``
 * is set.
 */
function authFetch(url, options = {}) {
    return fetch(url, { cache: 'no-store', credentials: 'include', ...options });
}

/**
 * Fetches the model catalogue for the Agent Configuration / Workflow tab
 * model dropdown from ABStudio's backend first:
 *
 *   GET ``/api/llm/models`` → LLM_PROXY_URL-backed catalogue aligned with CLI /v1/models
 *
 * Platform ``/all-models`` is only a fallback for standalone/degraded cases.
 * The selected model still saves to ``model_name`` / ``node.data.modelName``
 * and flows through the existing backend runtime wiring.
 *
 * Returns:
 *   - models:        flat string array (derived from providers when present)
 *   - providers:     grouped array for ``<optgroup>`` rendering (may be [])
 *   - defaultModel:  fallback selection (prefers RECOMMENDED_MODEL when present)
 *   - provider:      backend provider name ("ainxt" | "custom")
 *   - status:        one of MODEL_STATUS.*
 *   - error:         human-readable error message
 */

// Flatten a ``providers`` array into a deduped list of model IDs. Skips
// the ``auto`` pseudo-model when deriving the flat list since most
// consumers want a real model id as the fallback default.
function _flattenProviderIds(providers) {
    const seen = new Set();
    const out = [];
    for (const g of providers || []) {
        for (const m of (g && g.models) || []) {
            const id = (m && m.id) || m;
            if (id && !seen.has(id)) {
                seen.add(id);
                out.push(id);
            }
        }
    }
    return out;
}

// Apply the governance allowlist to the grouped catalogue using the EXACT
// same rule as the Chat sidebar:
//   - empty allowlist  → no restriction (return providers unchanged)
//   - ``auto`` is always kept (Chat treats it as a routing pseudo-model)
//   - real models filtered by ``id ∈ allowed``
// Empty groups are dropped so the dropdown doesn't render bare headings.
function _applyAllowlist(providers, allowed) {
    if (!Array.isArray(allowed) || allowed.length === 0) return providers;
    const allowSet = new Set(allowed);
    return (providers || [])
        .map(g => ({
            ...g,
            models: ((g && g.models) || []).filter(m => {
                const id = (m && m.id) || m;
                return id === 'auto' || allowSet.has(id);
            }),
        }))
        .filter(g => Array.isArray(g.models) && g.models.length > 0);
}

export default function useAvailableModels() {
    // Grouped catalogue from ABStudio /llm/models, with platform /all-models
    // used only as a fallback.
    const [allModelProviders, setAllModelProviders] = useState([]);
    // Optional frontend allowlist. The backend already applies governance on
    // /llm/models; apply this only when using platform /all-models fallback.
    const [allowedModels, setAllowedModels] = useState([]);
    const [usingPlatformFallback, setUsingPlatformFallback] = useState(false);

    // Track which fetches have settled so we can move from LOADING to
    // READY/EMPTY only once both have responded (or definitively failed).
    const [allLoaded, setAllLoaded]         = useState(false);
    const [allowedLoaded, setAllowedLoaded] = useState(false);
    const [fetchError, setFetchError]       = useState('');

    // Derived: providers filtered by the user's governance allowlist.
    // Pure function of the two pieces of state above — no extra state.
    const providers = useMemo(
        () => usingPlatformFallback
            ? _applyAllowlist(allModelProviders, allowedModels)
            : allModelProviders,
        [allModelProviders, allowedModels, usingPlatformFallback],
    );

    // Derived: flat list of model IDs from the filtered catalogue.
    const models = useMemo(() => _flattenProviderIds(providers), [providers]);

    // Derived: status — LOADING until both fetches have settled.
    const status = useMemo(() => {
        if (!allLoaded || !allowedLoaded) return MODEL_STATUS.LOADING;
        if (models.length > 0) return MODEL_STATUS.READY;
        if (fetchError) return MODEL_STATUS.ERROR;
        return MODEL_STATUS.EMPTY;
    }, [allLoaded, allowedLoaded, models, fetchError]);

    // Derived: default model — prefers RECOMMENDED_MODEL when available.
    const defaultModel = useMemo(
        () => pickDefaultModel(models, null),
        [models],
    );

    useEffect(() => {
        let cancelled = false;

        // ── ABStudio /llm/models ─ LLM_PROXY_URL-backed catalogue ───────────
        authFetch(`${API_BASE}/llm/models`)
            .then(r => (r.ok ? r.json() : null))
            .then(d => {
                if (cancelled) return;
                if (d && Array.isArray(d.providers) && d.providers.length > 0) {
                    setUsingPlatformFallback(false);
                    setAllModelProviders(d.providers);
                    return;
                }
                return authFetch(`${PLATFORM_API_BASE}/all-models`)
                    .then(r => (r.ok ? r.json() : null))
                    .then(fallback => {
                        if (cancelled) return;
                        if (fallback && Array.isArray(fallback.providers) && fallback.providers.length > 0) {
                            setUsingPlatformFallback(true);
                            setAllModelProviders(fallback.providers);
                        } else {
                            setFetchError(prev => prev || 'No models available');
                        }
                    });
            })
            .catch(() => {
                if (cancelled) return;
                return authFetch(`${PLATFORM_API_BASE}/all-models`)
                    .then(r => (r.ok ? r.json() : null))
                    .then(fallback => {
                        if (cancelled) return;
                        if (fallback && Array.isArray(fallback.providers) && fallback.providers.length > 0) {
                            setUsingPlatformFallback(true);
                            setAllModelProviders(fallback.providers);
                        } else {
                            setFetchError(prev => prev || 'Could not load models');
                        }
                    })
                    .catch(() => {
                        if (!cancelled) setFetchError(prev => prev || 'Could not load models');
                    });
            })
            .finally(() => {
                if (!cancelled) setAllLoaded(true);
            });

        // ── /model-governance/my-models ─ same call site as Chat.jsx:746 ──
        authFetch(`${PLATFORM_API_BASE}/model-governance/my-models`)
            .then(r => (r.ok ? r.json() : null))
            .then(d => {
                if (cancelled) return;
                if (d && Array.isArray(d.models)) {
                    setAllowedModels(d.models);
                }
                // Note: an empty/missing allowlist is fine — _applyAllowlist
                // treats it as "no restriction", same as Chat.jsx semantics.
            })
            .catch(() => {
                // Non-fatal: no allowlist means show everything (Chat does same).
            })
            .finally(() => {
                if (!cancelled) setAllowedLoaded(true);
            });

        return () => { cancelled = true; };
    }, []);

    return {
        models,
        providers,
        defaultModel,
        provider: DEFAULT_PROVIDER,
        status,
        error: fetchError,
    };
}
