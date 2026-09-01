// SPDX-License-Identifier: Apache-2.0
// Strip emoji / pictograph characters from text before it is rendered.
//
// Agent-generated copy frequently sprinkles palettes, push-pins, and other
// ornamental glyphs into headings and bullets which look unprofessional
// inside the workflow / agent preview panes. The regex covers the common
// Unicode emoji blocks (pictographs, symbols, dingbats, transport, flags,
// regional indicators, mahjong/playing-card tiles) plus the variation
// selectors and ZWJ that glue multi-codepoint emoji together.
//
// Plain ASCII content (incl. code blocks and hex colour swatches like
// `#0D1B3E`) is untouched. After stripping, we collapse any double spaces
// the removal left behind so " — " style separators stay clean.
export const EMOJI_REGEX = /[\u{1F1E6}-\u{1F1FF}\u{1F300}-\u{1F5FF}\u{1F600}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{1F700}-\u{1F77F}\u{1F780}-\u{1F7FF}\u{1F800}-\u{1F8FF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{FE00}-\u{FE0F}\u{1F000}-\u{1F02F}\u{1F0A0}-\u{1F0FF}\u{200D}]/gu;

export function stripEmoji(text) {
    if (text == null) return text;
    return String(text).replace(EMOJI_REGEX, '').replace(/[ \t]{2,}/g, ' ');
}

export default stripEmoji;
