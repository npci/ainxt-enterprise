// SPDX-License-Identifier: MIT
export function useMultiRepoEnabled() {
  return import.meta.env.VITE_ENABLE_MULTI_REPO_SDLC === 'true';
}
