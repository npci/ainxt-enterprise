// SPDX-License-Identifier: MIT
// messageContent.js — pure helpers for cleaning + classifying assistant
// message content. Shared between Chat.jsx (regular chat) and KbChat.jsx
// (KB-embedded chat) because both surfaces render the same assistant
// messages and must strip the same backend-emitted markers.
//
// All exports are pure functions with no React, no state, no side
// effects. Drop-in usable from any caller.

// Tones we currently detect from user-typed prompts. The pattern matches
// informal address terms across English + several Indian languages so we
// can adjust the assistant's tone when the user is being casual. Keep
// the list in lock-step with anything backend-side that recognises the
// same tones.
const CASUAL_PATTERNS = /\b(buddy|macha|machan|dei|da|bro|yaar|dude|man|mate|anna|bhai|re|enda|ayyo|dei da|boss|padam)\b/i;

// Strip the <!--MEMORY:{...}--> footer the backend appends to assistant
// content when it's emitting a memory hint. We have to handle three
// cases because the tag streams in over SSE:
//   1. Complete tag:           "...answer text\n<!--MEMORY:{...}-->"
//   2. Partial tag mid-stream: "...answer text\n<!--MEMORY:{\"store"
//   3. Very early partial:     "...answer text\n<!--MEM"
//   4. Truncated opener:       "...answer text\n<!--"
// "<!--MEM" is an unambiguous prefix — never appears in normal content — so we
// anchor on that and erase everything from there to the end. Case 4 needs its
// own pattern: the model can emit the bare opener and stop, which is shorter
// than "<!--MEM" and was therefore left visible at the end of the answer.
export function stripMemoryTag(content) {
  if (!content) return content;
  return content
    // The complete footer, or anything after it.
    .replace(/\n?<!--MEM[^]*$/s, "")
    // A TRUNCATED footer. The model sometimes emits just the opening "<!--"
    // (or a shorter prefix than "<!--MEM") and stops, and the pattern above
    // needs seven characters to match — so a bare "<!--" was left visible at
    // the end of the answer. Only a prefix of the marker followed by the end
    // of the content is matched, so this can never eat a real HTML comment or
    // anything inside a code block.
    .replace(/\n?<!--(?:M(?:E(?:M(?:O(?:R(?:Y:?)?)?)?)?)?)?\s*$/s, "");
}

// Parse the <!--MEMORY:{...}--> footer into a structured object so the UI can
// surface an inline "Memory updated" chip (Phase 3.2). Returns null when there
// is no *complete* memory tag, or when the model chose not to store anything
// (store:false). Only fires on the fully-streamed content, so a partial
// mid-stream tag never triggers a false chip.
//   → { store: true, summary: "prefers dark mode", context_key: "ui_theme" }
export function parseMemoryTag(content) {
  if (!content) return null;
  const m = content.match(/<!--MEMORY:(\{[^]*?\})-->/);
  if (!m) return null;
  try {
    const obj = JSON.parse(m[1]);
    if (!obj || obj.store !== true) return null;
    const summary = (obj.summary || "").toString().trim();
    if (!summary) return null;
    return {
      store: true,
      summary,
      context_key: (obj.context_key || "").toString().trim(),
    };
  } catch {
    return null;
  }
}

// Strip the [STYLE INSTRUCTION:…] or [CONTEXT:…] preamble the backend
// sometimes prepends to assistant content. We never want either visible
// to the user — they're internal routing signals.
export function stripSystemPrefix(content) {
  if (!content) return content;
  return content
    .replace(/^\[(STYLE INSTRUCTION|CONTEXT):[^\]]*\]\n\n?/g, "")
    .trimStart();
}

// Recover the user's actual question from a persisted USER message that had
// attachments. On send, the backend injects attachment context into the
// stored content in one of two shapes:
//   1. Documents (/ask):   "[File: a.pdf]\n<parsed text…>\n\nUser question: <q>"
//                          (possibly several [File:…] blocks concatenated)
//   2. Optimistic marker:  "<q>\n\n📎 file1, file2"  /  "<q>\n\n🖼 N images"
// Without this, a reloaded doc turn dumps the whole parsed PDF into the bubble
// (the chip below already represents the file). Returns just <q>, trimmed.
export function stripAttachmentContext(content) {
  if (!content) return content;
  let out = content;
  // Doc form: everything up to and including the last "User question:" label
  // is injected context — keep only what follows it.
  const uq = out.lastIndexOf("User question:");
  if (uq !== -1 && /^\s*\[File:/.test(out)) {
    out = out.slice(uq + "User question:".length);
  }
  // Optimistic marker form: trailing "\n\n📎 …" or "\n\n🖼 …" line.
  out = out.replace(/\n\n(?:📎|🖼)\s*.+$/s, "");
  return out.trim();
}

// Classify a user-typed prompt as "casual" iff it contains any of the
// informal-address terms in CASUAL_PATTERNS. Returns the tone string or
// null so callers can do `if (detectTone(text))` cleanly.
export function detectTone(text) {
  return CASUAL_PATTERNS.test(text) ? "casual" : null;
}
