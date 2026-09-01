// SPDX-License-Identifier: Apache-2.0
/**
 * Model defaults for the ABStudio frontend.
 *
 * The backend is authoritative: `useAvailableModels` fetches the catalogue a
 * deployment actually serves. These are the values needed BEFORE that arrives --
 * the recommended pick, and the seed model for a newly created LLM node.
 *
 * They were literals in five separate files, so a deployment running its own
 * models had to find and edit each one, and a new workflow node was seeded with
 * a model that deployment could not route to.
 *
 * Override at build time (Vite):
 *   VITE_ABS_RECOMMENDED_MODEL=my-org/llama-70b
 *   VITE_ABS_DEFAULT_NODE_MODEL=my-org/llama-70b
 *   VITE_ABS_DEFAULT_NODE_PROVIDER=custom
 */
const env = (name, fallback) => (import.meta.env[name] || fallback);

// The model suggested to a user when the catalogue offers no explicit default.
// No model is hardcoded — the UI defers to the first entry in the live
// catalogue returned by /llm/models when this is empty.
// Set VITE_ABS_RECOMMENDED_MODEL at build time to pin a specific model.
export const RECOMMENDED_MODEL = env('VITE_ABS_RECOMMENDED_MODEL', '');

// Seed values for a newly created LLM node.
// No model is hardcoded — the UI defers to the live catalogue when empty.
// Set VITE_ABS_DEFAULT_NODE_MODEL / VITE_ABS_DEFAULT_NODE_PROVIDER at build time.
export const DEFAULT_NODE_MODEL    = env('VITE_ABS_DEFAULT_NODE_MODEL', '');
export const DEFAULT_NODE_PROVIDER = env('VITE_ABS_DEFAULT_NODE_PROVIDER', '');

// The judge model seeded into an evaluation node.
export const DEFAULT_JUDGE_MODEL = env('VITE_ABS_JUDGE_MODEL', RECOMMENDED_MODEL);

// Legacy detection: workflows saved before the node defaults were configurable
// carry this exact combination, and the editor offers to migrate them. It is a
// historical marker, so it stays a literal on purpose -- reading it from
// configuration would stop it matching the rows it exists to recognise.
export const LEGACY_NODE_MODEL = 'gemini-2.5-flash';
