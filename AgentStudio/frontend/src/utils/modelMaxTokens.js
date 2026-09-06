// SPDX-License-Identifier: MIT
/**
 * Model → max-output-tokens mapping.
 *
 * Used by Agent Tab (AgentEditor.jsx) and Workflow Tab (ConfigPanel.jsx) to:
 *   1. Auto-set the Max Tokens field when the user selects a model.
 *   2. Cap the input's `max` attribute so users cannot type a value the
 *      model's API — or the backend — will reject.
 *
 * IMPORTANT: these are max-*output*-token limits, NOT context-window sizes.
 * The model's context window is the total input+output budget and is NOT
 * something the user sets here. `max_tokens` only bounds the generated response.
 * The backend (ABStudio/backend/app/models.py LLMConfig) hard-caps this at
 * `le=32000` for every model, so every value here is kept at or below
 * BACKEND_MAX_TOKENS_LIMIT and getMaxTokensForModel() clamps to it.
 *
 * Users can always *decrease* the value below the cap; they just cannot
 * exceed it.
 *
 * The lookup is namespace-agnostic — gateways often return ids like
 * `openai/gpt-5.5` or `anthropic/claude-sonnet-4-6`. We try the full id
 * first, then fall back to the trailing segment after the last `/`.
 */

// Backend hard limit (ABStudio/backend/app/models.py LLMConfig.max_tokens le=32000).
// The UI must never offer a value above this for any model.
export const BACKEND_MAX_TOKENS_LIMIT = 32000;

// Per-model max-OUTPUT-tokens caps. Used by the Agent Tab and Workflow Tab
// to set the upper bound of the Max Tokens control and auto-populate it
// when a model is selected. All values are <= BACKEND_MAX_TOKENS_LIMIT.
//
// NOTE: These are capability metadata entries, NOT hardcoded model defaults.
// No model is selected or recommended here — this table only caps the Max
// Tokens slider for models that the live /llm/models catalogue already serves.
// An OSS deployment running different models will simply get the fallback cap
// (DEFAULT_MAX_TOKENS) for any model id not listed here.
//
// Keep keys lowercase; lookup normalises before matching.
// Keys MUST match the model ids served by the backend catalogue
// (ABStudio/backend/app/api/generation.py :: _static_model_catalogue).
const MODEL_MAX_TOKENS = {
    // Claude
    'claude-sonnet-4-6':          32000,
    'claude-sonnet-5':            32000,
    'claude-opus-5':              32000,
    'claude-opus-4-8':            32000,
    'claude-opus-4-7':            32000,
    'claude-haiku-4-5':           32000,
    'claude-haiku-4-5-20251001':  32000,

    // GPT family
    'gpt-5.4':                    32000,
    'gpt-5-mini':                 32000,
    'gpt-5.5':                    32000,
    'gpt-5-5':                    32000,
    'gpt-5.6-tera':               32000,
    'gpt-5.6-luna':               32000,

    // Gemini
    'gemini-3.5-flash':           16384,
    'gemini-3.1-flash-lite':      16384,
    'gemini-3.1-flash-image':     8192,

    // Local / in-house. The dropdown's "Local (In-house)" option submits the
    // generic id `local`; specific served ids are also mapped for safety.
    'local':                      16384,
    'qwen-3.6-35b-a3b':           16384,
    'qwen-3.6-27b':               16384,
    'gemma-4-31b-it':             16384,
    'deepseek-v4-flash':          16384,
    'kimi-k2.6':                  16384,
    'glm-5.1-fp8':                16384,
};

// Fallback when the selected model isn't in the map. Conservative default
// matching what most OSS models ship with out of the box.
export const DEFAULT_MAX_TOKENS_CAP = 4096;

function _normalise(modelId) {
    if (!modelId || typeof modelId !== 'string') return '';
    return modelId.trim().toLowerCase();
}

// Models already reported as missing, so the warning fires once per model per
// page load rather than on every keystroke that recomputes the meter.
const _unknownLimitModels = new Set();

/**
 * Note that a context-window limit was defaulted for a model we have no entry
 * for. The meter still renders -- degrading is correct here -- but an operator
 * running their own models should be able to find out why the number looks off.
 */
function warnUnknownModelLimit(id, fallback) {
    const key = String(id || '(unset)');
    if (_unknownLimitModels.has(key)) return;
    _unknownLimitModels.add(key);
    try {
        console.warn(
            `modelMaxTokens: no context-window entry for model "${key}" - using ` +
            `default ${fallback}. The context-usage meter for this model is an ` +
            `estimate. Add it to MODEL_MAX_TOKENS in ` +
            `ABStudio/frontend/src/utils/modelMaxTokens.js.`
        );
    } catch (e) { /* never break rendering over a log line */ }
}

/**
 * Returns the max-output-tokens cap for the given model id, or
 * DEFAULT_MAX_TOKENS_CAP when the model is unknown.
 *
 * The result is always clamped to BACKEND_MAX_TOKENS_LIMIT so the UI can never
 * offer a value the backend (LLMConfig.max_tokens le=32000) will reject, even
 * if a future MODEL_MAX_TOKENS entry is set too high by mistake.
 */
export function getMaxTokensForModel(modelId) {
    const id = _normalise(modelId);

    let raw = DEFAULT_MAX_TOKENS_CAP;
    if (id) {
        // Strip a leading namespace (e.g. `anthropic/claude-haiku-4-5`) so
        // both bare and gateway-prefixed ids resolve the same way.
        const slash = id.lastIndexOf('/');
        const bare = slash !== -1 ? id.slice(slash + 1) : id;

        if (MODEL_MAX_TOKENS[id] != null) {
            raw = MODEL_MAX_TOKENS[id];
        } else if (MODEL_MAX_TOKENS[bare] != null) {
            raw = MODEL_MAX_TOKENS[bare];
        } else {
            // Prefix match: providers append date/version suffixes to ids
            // (e.g. `claude-haiku-4-5-20251001`). Resolve against the longest
            // known key the id starts with so suffixed variants still map.
            let best = '';
            for (const key of Object.keys(MODEL_MAX_TOKENS)) {
                if (bare.startsWith(key) && key.length > best.length) best = key;
            }
            if (best) {
                raw = MODEL_MAX_TOKENS[best];
            } else {
                // Unknown model. MODEL_MAX_TOKENS is keyed by the model ids this
                // project ships with; a deployment running its own models
                // (Ollama / vLLM / any other provider) will land here for every
                // one of them and silently receive the generic default. That
                // produces a wrong "% context used" meter with no indication it
                // is wrong, so say so once per model.
                warnUnknownModelLimit(id, raw);
            }
        }
    }

    return Math.min(raw, BACKEND_MAX_TOKENS_LIMIT);
}
