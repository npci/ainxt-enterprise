// SPDX-License-Identifier: Apache-2.0
export function useMultiRepoEnabled() {
  return import.meta.env.VITE_ENABLE_MULTI_REPO_SDLC === 'true';
}
