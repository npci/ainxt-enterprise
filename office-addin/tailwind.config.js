// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
/** Tailwind v3 config — MUST live at the project root so Vite/PostCSS pick it up.
 *  (A copy under src/taskpane/ is ignored by Vite and left the UI unstyled.) */
export default {
  content: ["./src/**/*.{js,jsx,ts,tsx,html}", "./index.html"],
  theme: { extend: {} },
  plugins: [],
};
