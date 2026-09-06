// SPDX-License-Identifier: MIT
import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end browser tests for the dynamic sub-agent system.
 *
 * The suite uses Playwright's built-in fixtures + route-mocking so a
 * test run does NOT require a live backend or LLM. Each spec serves
 * a tiny HTML harness from `e2e/harness/` that mounts the SSE reader
 * we ship in production, and intercepts `/agent-runner/chat-stream`
 * with a hand-crafted SSE stream so we exercise the exact wire
 * contract the backend emits.
 *
 * Run:
 *   npx playwright install chromium     # one-time
 *   npx playwright test                 # all specs
 *   npx playwright test --headed        # watch the browser
 *   npx playwright test e2e/workflow-tab.spec.ts  # one spec
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:5174',
    trace:   'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use:  { ...devices['Desktop Chrome'] },
    },
  ],
  // Boot a static file server that serves the e2e harness pages. We
  // intentionally do NOT use `npm run dev` here — the harness pages
  // are self-contained and standing up the full Vite dev server pulls
  // in the entire Build Studio (auth, store, etc.) which is overkill
  // for these contract tests.
  webServer: {
    command:        'npx http-server e2e/harness -p 5174 -s --cors',
    url:            'http://127.0.0.1:5174',
    reuseExistingServer: !process.env.CI,
    timeout:        30_000,
  },
});
