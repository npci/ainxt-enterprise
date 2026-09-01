// SPDX-License-Identifier: Apache-2.0
import { useState } from "react";
import { Sparkles } from "lucide-react";

/**
 * BrandMark — the official AiNxt logo mark (transparent PNG).
 * Single source of truth for the logo across sidebar, login, spinner, etc.
 *
 * Props:
 *   className — sizing / animation classes (e.g. "w-7 h-7", "w-4 h-4 brand-breathe")
 *   alt       — accessible label (default "AiNxt")
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

export default function BrandMark({ className = "w-7 h-7", alt = "AiNxt", ...rest }) {
  // Two-step degradation: SVG -> PNG -> glyph. A caller never sees a broken image.
  const [src, setSrc] = useState(BRAND_MARK_SRC);
  const [failed, setFailed] = useState(false);

  if (failed) {
    // Graceful fallback — an on-brand glyph that inherits the same sizing/
    // animation classes so callers don't need to know the image broke.
    return (
      <Sparkles
        aria-label={alt}
        className={`text-indigo-500 select-none ${className}`}
        {...rest}
      />
    );
  }

  return (
    <img
      src={src}
      alt={alt}
      draggable={false}
      onError={() =>
        src === BRAND_MARK_SRC ? setSrc(BRAND_MARK_FALLBACK) : setFailed(true)
      }
      className={`object-contain select-none ${className}`}
      {...rest}
    />
  );
}
