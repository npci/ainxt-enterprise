// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Dedicated config for the test runner. We intentionally do NOT extend
// vite.config.js here — the PostCSS prefix-selector plugin in the dev
// build rewrites selectors under `[data-ac]`, which would force every
// test to wrap rendered components in that scope. Tests don't care
// about scoping, just about behaviour.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.js'],
    include: ['src/**/__tests__/**/*.{test,spec}.{js,jsx,ts,tsx}'],
    exclude: ['node_modules', 'dist', 'e2e'],
  },
})
