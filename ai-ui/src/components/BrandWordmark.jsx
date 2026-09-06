// SPDX-License-Identifier: MIT
import { useState } from "react";

/**
 * BrandWordmark — the approved "AiNxt" wordmark artwork.
 *
 * Single source of truth for the wordmark, mirroring BrandMark. Both the sidebar
 * header and the login screen previously drew the wordmark as styled <span>
 * text ("AiNxt" + "Enterprise"), which meant the rendered brand depended on
 * whatever font the browser happened to resolve and drifted between the two
 * screens. This renders the supplied artwork instead.
 *
 * Props:
 *   className — sizing classes. Set a height and let width follow the aspect
 *               ratio (the artwork is 421 x 98.71, roughly 4.27:1).
 *   alt       — accessible label (default "AiNxt Enterprise")
 *
 * Degrades in two steps, SVG -> PNG -> text, so the brand never renders as a
 * broken image: the vector is preferred because the wordmark appears at heights
 * from 12px to 40px, the raster is the fallback, and if both fail the original
 * text treatment is used.
 */
const WORDMARK_SRC      = `${import.meta.env.BASE_URL}ainxt-wordmark.svg`;
const WORDMARK_FALLBACK = `${import.meta.env.BASE_URL}ainxt-wordmark.png`;

export default function BrandWordmark({
  className = "h-4",
  alt = "AiNxt Enterprise",
  textClassName = "text-gray-900",
  ...rest
}) {
  const [src, setSrc] = useState(WORDMARK_SRC);
  const [failed, setFailed] = useState(false);

  if (failed) {
    return (
      <span aria-label={alt} className={`font-bold tracking-tight ${textClassName}`} {...rest}>
        AiNxt
      </span>
    );
  }

  return (
    <img
      src={src}
      alt={alt}
      draggable={false}
      onError={() => (src === WORDMARK_SRC ? setSrc(WORDMARK_FALLBACK) : setFailed(true))}
      className={`w-auto object-contain select-none ${className}`}
      {...rest}
    />
  );
}
