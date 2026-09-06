// SPDX-License-Identifier: MIT
/**
 * SVG sanitiser for markup injected through dangerouslySetInnerHTML.
 *
 * Mermaid renders label text from a diagram definition straight into the SVG
 * it returns, and that definition arrives in assistant output — which is
 * influenceable through prompt injection or a poisoned RAG document. Injecting
 * the result verbatim would let a crafted diagram carry an event handler or a
 * nested <script> into the live DOM of an authenticated session (CWE-79).
 *
 * The sanitiser parses the markup and keeps only a positive allow-list of
 * structural/presentation SVG elements and attributes. Everything else —
 * script/foreignObject/animate/set/style/use elements, event handlers, style
 * attributes, and every href/xlink:href variant — is dropped, because a
 * deny-list of "known bad" names is trivially bypassed by SMIL animation
 * elements, <style> url(), embedded <foreignObject>, and namespace-prefixed
 * or entity-obfuscated scheme strings.
 *
 * Parsing is done with DOMParser rather than a regex; a parsed tree cannot be
 * fooled by the attribute-quoting and entity tricks that defeat pattern
 * matching on raw markup.
 */

// Only these SVG elements may survive. Everything else — foreignObject,
// animate, set, style, use, script, iframe, object, embed, etc. — is removed
// outright rather than sanitised in place.
const ALLOWED_ELEMENTS = new Set([
  "svg", "g", "path", "rect", "circle", "ellipse", "line",
  "polyline", "polygon", "text", "tspan", "textPath", "defs",
  "marker", "linearGradient", "radialGradient", "stop",
  "clipPath", "pattern", "mask", "symbol", "title", "desc",
]);

// Only these attributes may survive. This excludes every on* handler, the
// style attribute, and every href/xlink:href/src variant in one rule — no
// separate scheme-matching pass is needed because URL-bearing attributes are
// simply never in the allow-list.
const ALLOWED_ATTR_RE = new RegExp(
  "^(?:d|fill|fill-rule|fill-opacity|stroke|stroke-[a-z-]+|x|y|x1|y1|x2|y2|" +
  "cx|cy|r|rx|ry|width|height|viewBox|transform|points|opacity|font-[a-z-]+|" +
  "text-anchor|dominant-baseline|class|id|marker-[a-z-]+|offset|stop-color|" +
  "stop-opacity|gradientUnits|gradientTransform|patternUnits|patternTransform|" +
  "preserveAspectRatio|xmlns|xmlns:xlink)$"
);

function scrubElement(el) {
  for (const attr of Array.from(el.attributes || [])) {
    const name = attr.name.toLowerCase();
    // Positive allow-list: anything not explicitly permitted is dropped.
    // This kills on* handlers, style, SMIL from/to/values, and every
    // namespace-prefixed href variant (xl:href, xlink:href) in one rule.
    if (!ALLOWED_ATTR_RE.test(name)) el.removeAttribute(attr.name);
  }
}

/**
 * Return `markup` with its executable surface removed.
 *
 * Falls back to an empty string when the input cannot be parsed, so a failure
 * never results in unsanitised markup reaching the DOM.
 *
 * @param {string} markup Raw SVG markup.
 * @returns {string} Sanitised markup, or "" when the input is unusable.
 */
export function sanitizeSvg(markup) {
  if (!markup || typeof markup !== "string") return "";
  if (typeof DOMParser === "undefined") return "";

  try {
    const doc = new DOMParser().parseFromString(markup, "image/svg+xml");

    // A parse error means the markup is not well-formed SVG; refuse it rather
    // than guessing what the browser's lenient HTML parser would have made.
    if (doc.querySelector("parsererror")) return "";

    const root = doc.querySelector("svg");
    if (!root) return "";

    for (const el of Array.from(doc.querySelectorAll("*"))) {
      if (!ALLOWED_ELEMENTS.has(el.tagName?.toLowerCase())) {
        el.remove();
        continue;
      }
      scrubElement(el);
    }

    return new XMLSerializer().serializeToString(root);
  } catch (_) {
    return "";
  }
}

export default sanitizeSvg;
