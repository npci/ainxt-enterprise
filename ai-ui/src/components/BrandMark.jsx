// SPDX-License-Identifier: MIT
import { useState } from "react";
import { Sparkles } from "lucide-react";

/**
 * BrandMark — the official AiNxt logo mark (transparent PNG).
 * Single source of truth for the logo across sidebar, login, spinner, etc.
 *
 * Props:
 *   className — sizing / animation classes (e.g. "w-7 h-7", "w-4 h-4 brand-breathe")
 *   alt       — accessible label (default "AiNxt")
 *   plated    — render the mark on the navy brand plate with a soft glow, the
 *               treatment used on the login screen and the sidebar header. The
 *               plate takes `className` for its size; the mark is inset inside
 *               it. Left off for small inline uses (spinners, chat rows) where
 *               a filled square would read as a button.
 *
 * Served from public/ainxt-mark.svg (the approved icon artwork). The SVG is
 * preferred because the mark is rendered at sizes from 16px to 512px and vector
 * stays crisp at all of them; public/ainxt-mark.png is the raster fallback.
 * The URL is resolved against Vite's BASE_URL so it works both in dev ("/")
 * and in production, where the SPA is mounted under "/portal/". A bare
 * "/ainxt-mark.png" 404s in prod because it ignores the /portal base.
 *
 * Resilience: if the SVG fails to load (404, offline, corrupt) we try the PNG,
 * and if that fails too we fall back to a clean lucide <Sparkles> glyph instead
 * of the browser's broken-image icon. The brand mark never renders as a broken
 * box.
 */
// BASE_URL always ends in "/" (Vite guarantees this), so a simple join is safe.
const BRAND_MARK_SRC     = `${import.meta.env.BASE_URL}ainxt-mark.svg`;
const BRAND_MARK_FALLBACK = `${import.meta.env.BASE_URL}ainxt-mark.png`;

// Brand plate. Inline styles rather than Tailwind utilities because
// tailwind.config.js declares no custom colours, so the exact navy would
// otherwise have to be an arbitrary-value class repeated at every call site.
const PLATE_BG = "linear-gradient(155deg, #141d4e 0%, #1b3281 58%, #233a86 100%)";
const PLATE_LIFT =
  "radial-gradient(120% 95% at 80% 105%, rgba(43,70,158,0.90) 0%, rgba(43,70,158,0) 62%)";
const PLATE_GLOW =
  "radial-gradient(58% 58% at 50% 47%, rgba(203,222,255,0.30) 0%, rgba(203,222,255,0) 72%)";

export default function BrandMark({ className = "w-7 h-7", alt = "AiNxt", plated = false, ...rest }) {
  // Two-step degradation: SVG -> PNG -> glyph. A caller never sees a broken image.
  const [src, setSrc] = useState(BRAND_MARK_SRC);
  const [failed, setFailed] = useState(false);

  // Inside the plate the artwork is inset; unplated it fills the given box.
  const artClass = plated
    ? "w-[68%] h-[68%] relative z-10"
    : className;

  const art = failed ? (
    // Graceful fallback — an on-brand glyph that inherits the same sizing/
    // animation classes so callers don't need to know the image broke.
    <Sparkles
      aria-label={alt}
      className={`select-none ${plated ? "text-white/90" : "text-indigo-500"} ${artClass}`}
      {...(plated ? {} : rest)}
    />
  ) : (
    <img
      src={src}
      alt={alt}
      draggable={false}
      onError={() =>
        src === BRAND_MARK_SRC ? setSrc(BRAND_MARK_FALLBACK) : setFailed(true)
      }
      className={`object-contain select-none ${artClass}`}
      {...(plated ? {} : rest)}
    />
  );

  if (!plated) return art;

  return (
    <span
      className={`relative inline-flex items-center justify-center overflow-hidden rounded-[22%] ${className}`}
      style={{ background: PLATE_BG }}
      {...rest}
    >
      {/* Decorative only — the <img>/<Sparkles> above carries the label. */}
      <span aria-hidden="true" className="absolute inset-0" style={{ background: PLATE_LIFT }} />
      <span aria-hidden="true" className="absolute inset-0" style={{ background: PLATE_GLOW }} />
      {art}
    </span>
  );
}
