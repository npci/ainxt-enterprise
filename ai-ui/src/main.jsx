// SPDX-License-Identifier: MIT
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import 'highlight.js/styles/atom-one-light.css'
import App from './App.jsx'
import { PORTAL_BASE } from './config.js'

// ── URL tamper guard (works in both dev and prod) ─────────────────────────
// If browser URL doesn't start with /portal, redirect before React mounts.
// This runs once on page load — zero overhead after that.
const p = document.location.pathname;
const inPortal = p === PORTAL_BASE || p.startsWith(`${PORTAL_BASE}/`);

if (!inPortal) {
  document.location.replace(`${PORTAL_BASE}/`);
} else {
  if (p === PORTAL_BASE) {
    // Normalize in-place so we can still mount React on this same page load.
    // A full document.location.replace here triggered a reload race in dev
    // (Vite + SW) that left the page blank until a manual hard refresh.
    window.history.replaceState(null, '', `${PORTAL_BASE}/${document.location.search}${document.location.hash}`);
  }
  createRoot(document.getElementById('root')).render(
    <StrictMode>
      <BrowserRouter basename={PORTAL_BASE}>
        <App />
      </BrowserRouter>
    </StrictMode>,
  );
}
