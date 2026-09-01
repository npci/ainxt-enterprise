// SPDX-License-Identifier: Apache-2.0
// Strips the internal taxonomy tag (e.g. "[UC-70 | Viable-32 | instant tier] ")
// that backend templates prepend to their descriptions for searchability.
// The tag is useful in the API but should never reach the UI.
export function stripTemplateTag(text) {
    if (!text) return '';
    return text.replace(/^\s*\[[^\]]*\]\s*/, '').trim();
}
