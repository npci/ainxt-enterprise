// SPDX-License-Identifier: MIT
/**
 * OpenAI-compatible gateways frequently return model IDs in
 * `namespace/model-id` form (e.g. `openai/claude-sonnet-4-6`,
 * `anthropic/claude-3-5-sonnet`). The namespace is a routing hint for the
 * gateway, not part of the model name users want to see.
 *
 * Always persist the full id (so the backend still routes correctly) and only
 * use this helper for display.
 */
export function stripProviderPrefix(modelId) {
    if (!modelId) return '';
    return modelId.includes('/')
        ? modelId.split('/').slice(1).join('/')
        : modelId;
}
