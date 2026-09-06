// SPDX-License-Identifier: MIT
// Build Studio — single embeddable entry point.
//
// Host integration (separate route/page):
//
//   import BuildStudio from '<this-package>/src/BuildStudio.jsx';
//   <Route path="/build-studio" element={<BuildStudio />} />
//
// Everything is self-contained:
//   • Styles are imported here and scoped at build time to the [data-ac]
//     subtree (see vite.config.js), so they don't leak into the host.
//   • All layout lives inside .build-studio-root, which fills its parent
//     container (not the viewport) and pins any position:fixed elements to
//     itself — so nothing escapes onto the host chrome.
//
// Mount-point height contract:
//   The standard CSS contract is "give the mount point a height". When the
//   host gives us a bounded parent (height:100% chain, flex child with
//   bounded ancestor, or a fixed pixel height), that just works.
//
//   When the host DOESN'T provide a bounded parent (e.g. it wraps us in
//   a plain block with height:auto), `.build-studio-root { height:100% }`
//   collapses to `auto` and the internal scroll plumbing
//   (.dashboard-content-area { flex:1; overflow-y:auto }) never activates
//   because no ancestor is bounded.
//
//   To make embedding bulletproof regardless of host layout, we measure
//   the parent's bounding rect on mount + on resize and apply it as an
//   inline pixel height to `.build-studio-root`. This is iframe-equivalent
//   behavior (the iframe's own viewport gave the same anchor) without the
//   iframe drawbacks. If the parent IS bounded we just mirror its height
//   (no-op); if it isn't, we fall back to viewport height.
import { useEffect, useRef, useState } from 'react';
import './index.css';
import './light-theme.css';
import './agent-preview-polish.css';
import './workflow-editor-premium.css';
import './styles/triggers.css';

import App, { AppErrorBoundary } from './App.jsx';

export default function BuildStudio() {
  const rootRef = useRef(null);
  const [height, setHeight] = useState(null);

  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;

    const measure = () => {
      const parent = el.parentElement;
      // Prefer parent's measured height; fall back to viewport if parent
      // is also unbounded (parent height === 0 or matches our own height).
      const parentRect = parent?.getBoundingClientRect();
      const parentH = parentRect?.height || 0;
      const viewportH = window.innerHeight;
      const next = parentH > 0 ? parentH : viewportH;
      setHeight((prev) => (prev === next ? prev : next));
    };

    measure();

    const ro = new ResizeObserver(measure);
    if (el.parentElement) ro.observe(el.parentElement);
    window.addEventListener('resize', measure);

    return () => {
      ro.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, []);

  // Always provide an inline height so React Flow's first measurement isn't
  // 0×0 (which triggers Help: https://reactflow.dev/error#004). The
  // ResizeObserver replaces this with the parent's measured height on the
  // next tick — falling back to viewport keeps the canvas sized even when
  // the parent is unbounded.
  const wrapperStyle = { height: height ? `${height}px` : '100vh' };

  return (
    <div
      ref={rootRef}
      className="build-studio-root"
      data-ac=""
      style={wrapperStyle}
    >
      <AppErrorBoundary>
        <App />
      </AppErrorBoundary>
    </div>
  );
}
